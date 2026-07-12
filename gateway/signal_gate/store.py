import sqlite3
import time
import uuid
from dataclasses import dataclass

STATES = ("PREPARED", "PENDING", "PROCESSING", "INDETERMINATE", "APPLIED", "FAILED", "DELIVERY_FAILED", "EXPIRED", "SUPERSEDED")
TERMINAL = frozenset({"APPLIED", "FAILED", "DELIVERY_FAILED", "EXPIRED", "SUPERSEDED"})
ALLOWED = {
    "PREPARED": frozenset({"PENDING", "DELIVERY_FAILED", "SUPERSEDED"}),
    "PENDING": frozenset({"PROCESSING", "EXPIRED", "SUPERSEDED"}),
    "PROCESSING": frozenset({"INDETERMINATE", "APPLIED", "FAILED"}),
    "INDETERMINATE": frozenset({"INDETERMINATE", "APPLIED", "FAILED"}),
}


@dataclass(frozen=True)
class Txn:
    id: str
    state: str
    bill_user_id: str
    chat_id: str
    proposal_message_id: str | None
    proposal_sha256: str
    proposal_json: str
    expires_at: float
    request_id: str | None
    request_json: str | None
    approval_update_id: str | None


class Store:
    def __init__(self, path):
        self.path = str(path)
        from pathlib import Path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS proposal(
              id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              expires_at REAL NOT NULL, bill_user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
              source_update_id TEXT, source_message_id TEXT, proposal_message_id TEXT,
              display_sha256 TEXT NOT NULL, proposal_sha256 TEXT NOT NULL, proposal_json TEXT NOT NULL,
              approval_update_id TEXT, approval_message_id TEXT, request_id TEXT UNIQUE,
              request_json TEXT,
              m3_event_id TEXT, receipt_sha256 TEXT, error_code TEXT,
              CHECK(state IN ('PREPARED','PENDING','PROCESSING','INDETERMINATE','APPLIED','FAILED','DELIVERY_FAILED','EXPIRED','SUPERSEDED')));
            CREATE INDEX IF NOT EXISTS proposal_lane ON proposal(bill_user_id,chat_id,state,created_at);
            CREATE TABLE IF NOT EXISTS telegram_update(update_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, consumed_at REAL NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _txn(row):
        return Txn(row["id"], row["state"], row["bill_user_id"], row["chat_id"], row["proposal_message_id"], row["proposal_sha256"], row["proposal_json"], row["expires_at"], row["request_id"], row["request_json"], row["approval_update_id"])

    def prepare(self, *, bill_user_id, chat_id, source_update_id, source_message_id, display_sha256, proposal_sha256, proposal_json, ttl=1800):
        now, pid = time.time(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE proposal SET state='SUPERSEDED',updated_at=? WHERE bill_user_id=? AND chat_id=? AND state IN ('PREPARED','PENDING')", (now, str(bill_user_id), str(chat_id)))
            db.execute("INSERT INTO proposal(id,state,created_at,updated_at,expires_at,bill_user_id,chat_id,source_update_id,source_message_id,display_sha256,proposal_sha256,proposal_json) VALUES(?,'PREPARED',?,?,?,?,?,?,?,?,?,?)", (pid, now, now, now+ttl, str(bill_user_id), str(chat_id), None if source_update_id is None else str(source_update_id), None if source_message_id is None else str(source_message_id), display_sha256, proposal_sha256, proposal_json))
            db.execute("COMMIT")
        return pid

    def transition(self, pid, source, target, **values):
        if target not in ALLOWED.get(source, frozenset()):
            return False
        cols, args = ["state=?", "updated_at=?"], [target, time.time()]
        for key, value in values.items():
            cols.append(f"{key}=?"); args.append(value)
        args += [pid, source]
        with self.connect() as db:
            cur = db.execute(f"UPDATE proposal SET {','.join(cols)} WHERE id=? AND state=?", args)
            return cur.rowcount == 1

    def arm(self, pid, message_id):
        return self.transition(pid, "PREPARED", "PENDING", proposal_message_id=str(message_id))

    def delivery_failed(self, pid):
        return self.transition(pid, "PREPARED", "DELIVERY_FAILED", error_code="telegram_delivery")

    def eligible(self, bill_user_id, chat_id):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE proposal SET state='EXPIRED',updated_at=? WHERE state='PENDING' AND expires_at<=?", (now, now))
            rows = db.execute("SELECT * FROM proposal WHERE bill_user_id=? AND chat_id=? AND state='PENDING' AND expires_at>? ORDER BY created_at DESC", (str(bill_user_id), str(chat_id), now)).fetchall()
            db.execute("COMMIT")
        return [self._txn(row) for row in rows]

    def claim(self, pid, update_id, approval_message_id, request_id, request_json, reply_to_message_id=None):
        with self.connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO telegram_update VALUES(?,?,?)", (str(update_id), pid, time.time()))
                row = db.execute("SELECT * FROM proposal WHERE id=?", (pid,)).fetchone()
                if not row or row["state"] != "PENDING" or row["expires_at"] <= time.time():
                    db.execute("COMMIT"); return None
                if reply_to_message_id is not None and str(reply_to_message_id) != row["proposal_message_id"]:
                    db.execute("COMMIT"); return None
                cur = db.execute("UPDATE proposal SET state='PROCESSING',updated_at=?,approval_update_id=?,approval_message_id=?,request_id=?,request_json=? WHERE id=? AND state='PENDING'", (time.time(), str(update_id), str(approval_message_id), request_id, request_json, pid))
                if cur.rowcount != 1:
                    db.execute("ROLLBACK"); return None
                db.execute("COMMIT"); return request_id
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK"); return None

    def update_seen(self, update_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM telegram_update WHERE update_id=?", (str(update_id),)).fetchone() is not None

    def consume_unbound(self, update_id):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO telegram_update VALUES(?,?,?)", (str(update_id), "", time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    def get(self, pid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposal WHERE id=?", (pid,)).fetchone()
        return self._txn(row) if row else None

    def stale_inflight(self, before):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM proposal WHERE state IN ('PROCESSING','INDETERMINATE') AND updated_at<?", (before,)).fetchall()
        return [self._txn(row) for row in rows]

    def applied(self, pid, event_id, receipt_sha256):
        for source in ("PROCESSING", "INDETERMINATE"):
            if self.transition(pid, source, "APPLIED", m3_event_id=event_id, receipt_sha256=receipt_sha256):
                return True
        return False

    def rejected(self, pid, code):
        for source in ("PROCESSING", "INDETERMINATE"):
            if self.transition(pid, source, "FAILED", error_code=code):
                return True
        return False

    def indeterminate(self, pid, code):
        if self.transition(pid, "PROCESSING", "INDETERMINATE", error_code=code):
            return True
        return self.transition(pid, "INDETERMINATE", "INDETERMINATE", error_code=code)

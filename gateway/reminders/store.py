"""Local reminder state — pending confirmations, execution index, delivery attempts.

Three concerns, one SQLite (WAL) database:

1. ``proposal`` — the pending-confirmation state machine (mirrors the proven
   signal-gate store): a reminder mutation is PREPARED, armed to PENDING when the
   confirmation prompt is delivered, PROCESSING once Bill confirms, then APPLIED /
   FAILED / INDETERMINATE from the M3 outcome. This is what guarantees no
   canonical write before confirmation and duplicate-confirm protection.

2. ``execution`` — a *rebuildable* index linking a canonical ``reminder_id`` to
   the local one-shot cron job. It holds only execution facts (version, due,
   delivery target, cron job id) — never the canonical approval authority. M3 is
   the source of truth; :func:`reconcile` rebuilds this from REMINDER_LIST.

3. ``delivery`` — per-attempt rows keyed by a deterministic ``attempt_id`` so a
   replayed scheduler tick or a restart cannot deliver twice or double-record.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

STATES = ("PREPARED", "PENDING", "PROCESSING", "INDETERMINATE",
          "APPLIED", "FAILED", "DELIVERY_FAILED", "EXPIRED", "SUPERSEDED")
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
    action: str
    bill_user_id: str
    chat_id: str
    reminder_id: Optional[str]
    base_version: Optional[int]
    proposal_message_id: Optional[str]
    proposal_sha256: str
    proposal_json: str
    expires_at: float
    request_id: Optional[str]
    request_json: Optional[str]


@dataclass(frozen=True)
class Execution:
    reminder_id: str
    version: int
    status: str
    text: str
    due_rfc3339: Optional[str]
    timezone: str
    target_json: str
    cron_job_id: Optional[str]


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS proposal(
              id TEXT PRIMARY KEY, state TEXT NOT NULL, action TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
              bill_user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
              reminder_id TEXT, base_version INTEGER,
              source_update_id TEXT, proposal_message_id TEXT,
              proposal_sha256 TEXT NOT NULL, proposal_json TEXT NOT NULL,
              approval_update_id TEXT, approval_message_id TEXT,
              request_id TEXT UNIQUE, request_json TEXT,
              m3_event_id TEXT, receipt_sha256 TEXT, error_code TEXT, error_detail TEXT,
              CHECK(state IN ('PREPARED','PENDING','PROCESSING','INDETERMINATE','APPLIED','FAILED','DELIVERY_FAILED','EXPIRED','SUPERSEDED')));
            CREATE INDEX IF NOT EXISTS proposal_lane ON proposal(bill_user_id,chat_id,state,created_at);
            CREATE TABLE IF NOT EXISTS telegram_update(update_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, consumed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS execution(
              reminder_id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
              text TEXT NOT NULL, due_rfc3339 TEXT, timezone TEXT NOT NULL,
              target_json TEXT NOT NULL, cron_job_id TEXT, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery(
              attempt_id TEXT PRIMARY KEY, reminder_id TEXT NOT NULL, version INTEGER NOT NULL,
              state TEXT NOT NULL, outcome TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              CHECK(state IN ('CLAIMED','RECORDED')));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    # --- proposal state machine --------------------------------------------

    @staticmethod
    def _txn(row) -> Txn:
        return Txn(
            row["id"], row["state"], row["action"], row["bill_user_id"], row["chat_id"],
            row["reminder_id"], row["base_version"], row["proposal_message_id"],
            row["proposal_sha256"], row["proposal_json"], row["expires_at"],
            row["request_id"], row["request_json"],
        )

    def prepare(self, *, action, bill_user_id, chat_id, reminder_id, base_version,
                source_update_id, proposal_sha256, proposal_json, ttl=1800) -> str:
        """Stage a mutation as PREPARED, superseding any earlier open proposal in
        the same lane so only one reminder proposal is ever pending at once."""
        now, pid = time.time(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE proposal SET state='SUPERSEDED',updated_at=? "
                "WHERE bill_user_id=? AND chat_id=? AND state IN ('PREPARED','PENDING')",
                (now, str(bill_user_id), str(chat_id)),
            )
            db.execute(
                "INSERT INTO proposal(id,state,action,created_at,updated_at,expires_at,"
                "bill_user_id,chat_id,reminder_id,base_version,source_update_id,"
                "proposal_sha256,proposal_json) "
                "VALUES(?,'PREPARED',?,?,?,?,?,?,?,?,?,?,?)",
                (pid, action, now, now, now + ttl, str(bill_user_id), str(chat_id),
                 reminder_id, base_version,
                 None if source_update_id is None else str(source_update_id),
                 proposal_sha256, proposal_json),
            )
            db.execute("COMMIT")
        return pid

    def transition(self, pid, source, target, **values) -> bool:
        if target not in ALLOWED.get(source, frozenset()):
            return False
        cols, args = ["state=?", "updated_at=?"], [target, time.time()]
        for key, value in values.items():
            cols.append(f"{key}=?")
            args.append(value)
        args += [pid, source]
        with self.connect() as db:
            cur = db.execute(
                f"UPDATE proposal SET {','.join(cols)} WHERE id=? AND state=?", args
            )
            return cur.rowcount == 1

    def arm(self, pid, message_id) -> bool:
        return self.transition(pid, "PREPARED", "PENDING", proposal_message_id=str(message_id))

    def delivery_failed(self, pid) -> bool:
        return self.transition(pid, "PREPARED", "DELIVERY_FAILED", error_code="telegram_delivery")

    def supersede(self, pid) -> bool:
        return self.transition(pid, "PENDING", "SUPERSEDED")

    def eligible(self, bill_user_id, chat_id) -> List[Txn]:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE proposal SET state='EXPIRED',updated_at=? WHERE state='PENDING' AND expires_at<=?",
                (now, now),
            )
            rows = db.execute(
                "SELECT * FROM proposal WHERE bill_user_id=? AND chat_id=? "
                "AND state='PENDING' AND expires_at>? ORDER BY created_at DESC",
                (str(bill_user_id), str(chat_id), now),
            ).fetchall()
            db.execute("COMMIT")
        return [self._txn(r) for r in rows]

    def claim(self, pid, update_id, approval_message_id, request_id, request_json,
              reply_to_message_id=None):
        """Atomically consume the confirming Telegram update and move PENDING→
        PROCESSING. Returns the request_id on success, None if already handled."""
        with self.connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO telegram_update VALUES(?,?,?)", (str(update_id), pid, time.time()))
                row = db.execute("SELECT * FROM proposal WHERE id=?", (pid,)).fetchone()
                if not row or row["state"] != "PENDING" or row["expires_at"] <= time.time():
                    db.execute("COMMIT")
                    return None
                if reply_to_message_id is not None and str(reply_to_message_id) != row["proposal_message_id"]:
                    db.execute("COMMIT")
                    return None
                cur = db.execute(
                    "UPDATE proposal SET state='PROCESSING',updated_at=?,approval_update_id=?,"
                    "approval_message_id=?,request_id=?,request_json=? WHERE id=? AND state='PENDING'",
                    (time.time(), str(update_id), str(approval_message_id), request_id, request_json, pid),
                )
                if cur.rowcount != 1:
                    db.execute("ROLLBACK")
                    return None
                db.execute("COMMIT")
                return request_id
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                return None

    def update_seen(self, update_id) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM telegram_update WHERE update_id=?", (str(update_id),)
            ).fetchone() is not None

    def consume_unbound(self, update_id) -> bool:
        with self.connect() as db:
            try:
                db.execute("INSERT INTO telegram_update VALUES(?,?,?)", (str(update_id), "", time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    def get(self, pid) -> Optional[Txn]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposal WHERE id=?", (pid,)).fetchone()
        return self._txn(row) if row else None

    def stale_inflight(self, before) -> List[Txn]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM proposal WHERE state IN ('PROCESSING','INDETERMINATE') AND updated_at<?",
                (before,),
            ).fetchall()
        return [self._txn(r) for r in rows]

    def applied(self, pid, event_id, receipt_sha256) -> bool:
        for source in ("PROCESSING", "INDETERMINATE"):
            if self.transition(pid, source, "APPLIED", m3_event_id=event_id, receipt_sha256=receipt_sha256):
                return True
        return False

    def rejected(self, pid, code, detail=None) -> bool:
        for source in ("PROCESSING", "INDETERMINATE"):
            if self.transition(pid, source, "FAILED", error_code=code, error_detail=detail):
                return True
        return False

    def indeterminate(self, pid, code) -> bool:
        if self.transition(pid, "PROCESSING", "INDETERMINATE", error_code=code):
            return True
        return self.transition(pid, "INDETERMINATE", "INDETERMINATE", error_code=code)

    # --- execution index (rebuildable from M3) -----------------------------

    @staticmethod
    def _execution(row) -> Execution:
        return Execution(
            row["reminder_id"], row["version"], row["status"], row["text"],
            row["due_rfc3339"], row["timezone"], row["target_json"], row["cron_job_id"],
        )

    def upsert_execution(self, *, reminder_id, version, status, text, due_rfc3339,
                         timezone, target_json, cron_job_id=None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO execution(reminder_id,version,status,text,due_rfc3339,timezone,"
                "target_json,cron_job_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(reminder_id) DO UPDATE SET version=excluded.version,"
                "status=excluded.status,text=excluded.text,due_rfc3339=excluded.due_rfc3339,"
                "timezone=excluded.timezone,target_json=excluded.target_json,"
                "cron_job_id=COALESCE(excluded.cron_job_id,execution.cron_job_id),"
                "updated_at=excluded.updated_at",
                (reminder_id, version, status, text, due_rfc3339, timezone,
                 target_json, cron_job_id, time.time()),
            )

    def set_cron_job(self, reminder_id, cron_job_id) -> None:
        with self.connect() as db:
            db.execute("UPDATE execution SET cron_job_id=?,updated_at=? WHERE reminder_id=?",
                       (cron_job_id, time.time(), reminder_id))

    def get_execution(self, reminder_id) -> Optional[Execution]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM execution WHERE reminder_id=?", (reminder_id,)).fetchone()
        return self._execution(row) if row else None

    def list_executions(self) -> List[Execution]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM execution ORDER BY due_rfc3339").fetchall()
        return [self._execution(r) for r in rows]

    def delete_execution(self, reminder_id) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM execution WHERE reminder_id=?", (reminder_id,))

    # --- delivery attempts (idempotency + duplicate-tick protection) -------

    def claim_attempt(self, attempt_id, reminder_id, version) -> bool:
        """Atomically claim one delivery attempt. Returns True only for the first
        caller; a replayed tick or restart gets False and must not deliver."""
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO delivery(attempt_id,reminder_id,version,state,created_at,updated_at) "
                    "VALUES(?,?,?,'CLAIMED',?,?)",
                    (attempt_id, reminder_id, version, time.time(), time.time()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def attempt_state(self, attempt_id) -> Optional[str]:
        with self.connect() as db:
            row = db.execute("SELECT state,outcome FROM delivery WHERE attempt_id=?", (attempt_id,)).fetchone()
        return None if row is None else row["state"]

    def record_attempt(self, attempt_id, outcome) -> None:
        with self.connect() as db:
            db.execute("UPDATE delivery SET state='RECORDED',outcome=?,updated_at=? WHERE attempt_id=?",
                       (outcome, time.time(), attempt_id))

    def release_attempt(self, attempt_id) -> None:
        """Drop a claim that never reached delivery (e.g. M3 unverifiable at fire
        time) so a later tick or restart reconciliation can retry it."""
        with self.connect() as db:
            db.execute("DELETE FROM delivery WHERE attempt_id=? AND state='CLAIMED'", (attempt_id,))

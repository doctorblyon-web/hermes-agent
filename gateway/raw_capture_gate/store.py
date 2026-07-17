import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Transaction:
    request_id: str
    state: str
    request_json: str
    event_id: str | None
    receipt_sha256: str | None
    error_code: str | None
    attempt_count: int


class Store:
    """Durable reconciliation state; M3 remains canonical for captures."""

    def __init__(self, path):
        self.path = str(path)
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        self._repair_modes()
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS capture_request(
                  request_id TEXT PRIMARY KEY,
                  source_update_id TEXT NOT NULL UNIQUE,
                  source_message_id TEXT NOT NULL,
                  request_sha256 TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  event_id TEXT,
                  receipt_sha256 TEXT,
                  error_code TEXT,
                  CHECK(state IN ('PROCESSING','INDETERMINATE','APPLIED','FAILED'))
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(capture_request)")}
            if "attempt_count" not in columns:
                db.execute(
                    "ALTER TABLE capture_request ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                )
        self._repair_modes()

    def _repair_modes(self):
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                pass

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        self._repair_modes()
        return db

    @staticmethod
    def _txn(row):
        return Transaction(
            row["request_id"], row["state"], row["request_json"], row["event_id"],
            row["receipt_sha256"], row["error_code"], row["attempt_count"],
        )

    def begin(self, *, request_id, update_id, message_id, request_sha256, request_json):
        now = time.time()
        with self.connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM capture_request WHERE source_update_id=?", (str(update_id),)
                ).fetchone()
                if row:
                    db.execute("COMMIT")
                    return self._txn(row), row["request_sha256"] == request_sha256
                db.execute(
                    """INSERT INTO capture_request(
                         request_id,source_update_id,source_message_id,request_sha256,
                         request_json,state,created_at,updated_at,event_id,receipt_sha256,
                         error_code,attempt_count
                       ) VALUES(?,?,?,?,?,'PROCESSING',?,?,?,?,?,0)""",
                    (request_id, str(update_id), str(message_id), request_sha256,
                     request_json, now, now, None, None, None),
                )
                db.execute("COMMIT")
                return self.get(request_id), True
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM capture_request WHERE request_id=?", (request_id,)).fetchone()
        return self._txn(row) if row else None

    def inflight(self, *, limit=8, max_attempts=4):
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM capture_request
                   WHERE state IN ('PROCESSING','INDETERMINATE') AND attempt_count < ?
                   ORDER BY created_at LIMIT ?""",
                (max_attempts, limit),
            ).fetchall()
        return [self._txn(row) for row in rows]

    def record_attempt(self, request_id):
        with self.connect() as db:
            cur = db.execute(
                """UPDATE capture_request
                   SET attempt_count=attempt_count+1,updated_at=?
                   WHERE request_id=? AND state IN ('PROCESSING','INDETERMINATE')""",
                (time.time(), request_id),
            )
        return cur.rowcount == 1

    def transition(self, request_id, states, target, **values):
        cols, args = ["state=?", "updated_at=?"], [target, time.time()]
        for key, value in values.items():
            cols.append(f"{key}=?")
            args.append(value)
        placeholders = ",".join("?" for _ in states)
        args.extend([request_id, *states])
        with self.connect() as db:
            cur = db.execute(
                f"UPDATE capture_request SET {','.join(cols)} WHERE request_id=? AND state IN ({placeholders})",
                args,
            )
        return cur.rowcount == 1

    def applied(self, request_id, event_id, receipt_sha256):
        return self.transition(
            request_id, ("PROCESSING", "INDETERMINATE"), "APPLIED",
            event_id=event_id, receipt_sha256=receipt_sha256, error_code=None,
        )

    def failed(self, request_id, code):
        return self.transition(
            request_id, ("PROCESSING", "INDETERMINATE"), "FAILED", error_code=code,
        )

    def indeterminate(self, request_id, code):
        return self.transition(
            request_id, ("PROCESSING", "INDETERMINATE"), "INDETERMINATE", error_code=code,
        )

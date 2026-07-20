"""Local PA-object idempotency ledger.

M3 remains canonical for every object, version and status. This store's only job
is to bind one Telegram update to one M3 request so a replayed update (a gateway
re-delivery or a restart) can never create or transition a *second* object:

* ``begin`` records the update_id with the request_id and request bytes Williams
  intends to send, once. A replay of the same update returns the existing row and
  the request_id/JSON already chosen, so the retried M3 call carries the SAME
  request_id and M3's operation-ledger returns the identical canonical result —
  never a duplicate. A replay whose recomputed request bytes differ is refused.
* ``applied`` / ``failed`` / ``indeterminate`` record the terminal local outcome
  and, on success, the canonical object_id and the exact confirmation text, so an
  already-applied replay is answered from cache without touching M3 at all.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Txn:
    update_id: str
    state: str
    request_id: str
    request_sha256: str
    request_json: str
    kind: Optional[str]
    action: Optional[str]
    object_id: Optional[str]
    reply_text: Optional[str]
    error_code: Optional[str]


class Store:
    def __init__(self, path):
        self.path = str(path)
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS pa_object_txn(
                  update_id TEXT PRIMARY KEY,
                  state TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  request_sha256 TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  kind TEXT, action TEXT,
                  object_id TEXT, reply_text TEXT, error_code TEXT,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  CHECK(state IN ('PROCESSING','APPLIED','FAILED','INDETERMINATE'))
                );
                """
            )
        self._chmod()

    def _chmod(self):
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
        return db

    @staticmethod
    def _txn(row) -> Txn:
        return Txn(
            row["update_id"], row["state"], row["request_id"], row["request_sha256"],
            row["request_json"], row["kind"], row["action"], row["object_id"],
            row["reply_text"], row["error_code"],
        )

    def begin(self, *, update_id, request_id, request_sha256, request_json, kind, action):
        """Bind one Telegram update. Returns (txn, identical): identical is False
        only when this update is already bound to *different* request bytes."""
        now = time.time()
        try:
            with self.connect() as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT * FROM pa_object_txn WHERE update_id=?", (str(update_id),)
                    ).fetchone()
                    if row:
                        db.execute("COMMIT")
                        return self._txn(row), row["request_sha256"] == request_sha256
                    db.execute(
                        "INSERT INTO pa_object_txn(update_id,state,request_id,request_sha256,"
                        "request_json,kind,action,object_id,reply_text,error_code,created_at,updated_at)"
                        " VALUES(?,'PROCESSING',?,?,?,?,?,NULL,NULL,NULL,?,?)",
                        (str(update_id), request_id, request_sha256, request_json,
                         kind, action, now, now),
                    )
                    db.execute("COMMIT")
                    return self.get(update_id), True
                except Exception:
                    db.execute("ROLLBACK")
                    raise
        finally:
            self._chmod()

    def get(self, update_id) -> Optional[Txn]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM pa_object_txn WHERE update_id=?", (str(update_id),)
            ).fetchone()
        return self._txn(row) if row else None

    def _to(self, update_id, states, target, **values):
        cols, args = ["state=?", "updated_at=?"], [target, time.time()]
        for key, value in values.items():
            cols.append(f"{key}=?")
            args.append(value)
        placeholders = ",".join("?" for _ in states)
        args.extend([str(update_id), *states])
        with self.connect() as db:
            cur = db.execute(
                f"UPDATE pa_object_txn SET {','.join(cols)} WHERE update_id=? AND state IN ({placeholders})",
                args,
            )
        return cur.rowcount == 1

    def applied(self, update_id, object_id, reply_text) -> bool:
        return self._to(update_id, ("PROCESSING", "INDETERMINATE"), "APPLIED",
                        object_id=object_id, reply_text=reply_text, error_code=None)

    def failed(self, update_id, code) -> bool:
        return self._to(update_id, ("PROCESSING", "INDETERMINATE"), "FAILED", error_code=code)

    def indeterminate(self, update_id, code) -> bool:
        return self._to(update_id, ("PROCESSING", "INDETERMINATE"), "INDETERMINATE", error_code=code)

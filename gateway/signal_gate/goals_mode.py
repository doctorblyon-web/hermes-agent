"""A tiny, explicit Goals conversation-state gate.

Goals mode is *armed* for the Bill lane only in one situation: Christine has just
asked Bill for today's goals (because he opened Goals without stating them) and is
waiting for his answer. While armed, Bill's next message is treated as his goals.
The window is one-shot and time-boxed: it is consumed by the very next message in
the lane and it expires after a short TTL, so it can never silently swallow a
later ordinary message.

This is deliberately small — a single per-lane flag with an expiry — not an
intent classifier. It never touches the canonical signal store or M3.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from hermes_constants import get_hermes_home

DEFAULT_TTL = 1800  # 30 minutes, matching the goals proposal TTL


def _db_path(cfg) -> Path:
    path = (cfg or {}).get("goals_mode_db") or str(
        get_hermes_home() / "signal_gate" / "goals_mode.sqlite"
    )
    return Path(path).expanduser()


def _connect(cfg):
    path = _db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS goals_mode("
        "lane TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
    )
    for candidate in (str(path), str(path) + "-wal", str(path) + "-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            pass
    return db


def _lane(source) -> str:
    return f"{getattr(source, 'user_id', None)}:{getattr(source, 'chat_id', None)}"


def arm(cfg, source, *, ttl=DEFAULT_TTL) -> None:
    """Arm the one-shot goals window for this lane."""
    with _connect(cfg) as db:
        db.execute(
            "INSERT INTO goals_mode(lane,expires_at) VALUES(?,?) "
            "ON CONFLICT(lane) DO UPDATE SET expires_at=excluded.expires_at",
            (_lane(source), time.time() + ttl),
        )


def is_armed(cfg, source, *, now=None) -> bool:
    """True only if armed and not expired. Expired rows are pruned."""
    now = time.time() if now is None else now
    with _connect(cfg) as db:
        row = db.execute(
            "SELECT expires_at FROM goals_mode WHERE lane=?", (_lane(source),)
        ).fetchone()
        if row is None:
            return False
        if row[0] <= now:
            db.execute("DELETE FROM goals_mode WHERE lane=?", (_lane(source),))
            return False
        return True


def disarm(cfg, source) -> None:
    """Consume/clear the window (on the next message, or on end of Goals)."""
    with _connect(cfg) as db:
        db.execute("DELETE FROM goals_mode WHERE lane=?", (_lane(source),))

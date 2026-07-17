"""Focused tests for the bounded SIGNAL rejection-observability correction.

When M3 returns a REJECTED response, Christine must durably retain M3's exact
error.code and error.message for diagnosis, while the Telegram reply stays plain
and free of codes, IDs, JSON, hashes and raw receipts. Successful SIGNAL
behaviour must be unchanged.
"""

import asyncio
import sqlite3

from gateway.signal_gate import service
from gateway.signal_gate.store import Store

# Both goals and plain SIGNAL failures now report the same plain goals message;
# success reports the required plain confirmation.
GOALS_REJECT_TEXT = "I couldn’t update today’s goals. Nothing was changed."
PLAIN_REJECT_TEXT = "I couldn’t update today’s goals. Nothing was changed."
GOALS_SUCCESS_TEXT = "Done. Today’s goals are updated."


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text, reply_to))
        return type("R", (), {
            "success": True, "message_id": "1",
            "continuation_message_ids": (), "raw_response": {},
        })()


def _row(store, pid):
    with store.connect() as db:
        return db.execute(
            "SELECT state, error_code, error_detail, m3_event_id, receipt_sha256 "
            "FROM proposal WHERE id=?", (pid,),
        ).fetchone()


def _plain_processing_txn(store):
    pid = store.prepare(
        bill_user_id="bill", chat_id="chat",
        source_update_id="su1", source_message_id="sm1",
        display_sha256="d" * 64, proposal_sha256="p" * 64,
        proposal_json='{"actions":[],"no_today":null}',
    )
    store.arm(pid, "100")
    store.claim(pid, "200", "201", "req-plain", "{}")
    return store.get(pid)


def _goals_processing_txn(store):
    store.stage_today_goals(
        source_update_id="gu1", source_message_id="gm1",
        source_sha256="s" * 64, proposal_sha256="q" * 64,
        capture_json="{}", target_date="2026-07-17",
        expected_state_sha256="e" * 64, replacement=1,
    )
    pid = store.prepare_today_goals(
        source_update_id="gu1", bill_user_id="bill", chat_id="chat",
        display_sha256="d" * 64, proposal_sha256="q" * 64,
        proposal_json='{"actions":[{"mission":"billos","text":"x"}],"no_today":null}',
    )
    assert pid
    store.arm(pid, "300")
    store.claim(pid, "400", "401", "req-goals", "{}")
    return store.get(pid)


def _apply(monkeypatch, store, txn, response):
    async def fake_call(request, timeout=12):
        return response

    monkeypatch.setattr(service.m3_client, "call", fake_call)
    adapter = FakeAdapter()
    asyncio.run(service._apply(adapter, store, txn, {"request_id": "r"}, reply_to="500"))
    return adapter


def test_rejected_retains_code_and_message_for_goals(tmp_path, monkeypatch):
    store = Store(tmp_path / "sg.db")
    txn = _goals_processing_txn(store)
    detail = "canonical precondition failed at ledger: expected abc got def"
    adapter = _apply(monkeypatch, store, txn, {
        "status": "REJECTED", "applied": False,
        "error": {"code": "process_event_rejected", "message": detail},
    })

    row = _row(store, txn.id)
    # error.code AND error.message retained in the durable SIGNAL transaction.
    assert row["state"] == "FAILED"
    assert row["error_code"] == "process_event_rejected"
    assert row["error_detail"] == detail

    # User-facing text is plain and leaks no internal detail.
    assert len(adapter.sent) == 1
    _, text, _ = adapter.sent[0]
    assert text == GOALS_REJECT_TEXT
    for leak in ("process_event_rejected", "canonical precondition",
                 "expected abc", "ledger", "{", "}", txn.id):
        assert leak not in text


def test_rejected_retains_detail_for_plain_signal(tmp_path, monkeypatch):
    store = Store(tmp_path / "sg.db")
    txn = _plain_processing_txn(store)
    adapter = _apply(monkeypatch, store, txn, {
        "status": "REJECTED", "applied": False,
        "error": {"code": "schema", "message": "raw internal boundary detail"},
    })

    row = _row(store, txn.id)
    assert row["state"] == "FAILED"
    assert row["error_code"] == "schema"
    assert row["error_detail"] == "raw internal boundary detail"

    _, text, _ = adapter.sent[0]
    assert text == PLAIN_REJECT_TEXT
    assert "schema" not in text and "raw internal boundary detail" not in text


def test_rejected_without_error_message_defaults_safely(tmp_path, monkeypatch):
    store = Store(tmp_path / "sg.db")
    txn = _plain_processing_txn(store)
    adapter = _apply(monkeypatch, store, txn, {"status": "REJECTED", "applied": False})

    row = _row(store, txn.id)
    assert row["state"] == "FAILED"
    assert row["error_code"] == "m3_rejected"
    assert row["error_detail"] is None
    assert adapter.sent[0][1] == PLAIN_REJECT_TEXT


def test_success_behaviour_unchanged(tmp_path, monkeypatch):
    store = Store(tmp_path / "sg.db")
    txn = _goals_processing_txn(store)
    adapter = _apply(monkeypatch, store, txn, {
        "status": "APPLIED", "event_id": "EV1",
        "process_receipt": {"receipt_sha256": "a" * 64},
    })

    row = _row(store, txn.id)
    assert row["state"] == "APPLIED"
    assert row["m3_event_id"] == "EV1"
    assert row["receipt_sha256"] == "a" * 64
    assert row["error_detail"] is None  # untouched on success
    assert adapter.sent[0][1] == GOALS_SUCCESS_TEXT


def test_migration_adds_error_detail_to_preexisting_db(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        """CREATE TABLE proposal(
             id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL NOT NULL,
             updated_at REAL NOT NULL, expires_at REAL NOT NULL,
             bill_user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
             source_update_id TEXT, source_message_id TEXT, proposal_message_id TEXT,
             display_sha256 TEXT NOT NULL, proposal_sha256 TEXT NOT NULL,
             proposal_json TEXT NOT NULL, approval_update_id TEXT,
             approval_message_id TEXT, request_id TEXT UNIQUE, request_json TEXT,
             m3_event_id TEXT, receipt_sha256 TEXT, error_code TEXT);"""
    )
    con.close()

    before = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(proposal)")}
    assert "error_detail" not in before

    Store(path)  # opening runs the additive, idempotent migration

    after = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(proposal)")}
    assert "error_detail" in after

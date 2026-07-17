"""End-to-end-ish tests for the Goals interaction repair.

Covers the three live-canary failures:
  A. raw JSON / ```signal envelope shown to Telegram  -> render_human only;
  B. only literal "Y" accepted                        -> natural confirmation;
  C. natural "Yes" discarded                          -> flexible parsing.

The M3 transport is stubbed; these tests assert Christine-side behaviour only.
"""

import asyncio

import pytest

from gateway.signal_gate import schema, service
from gateway.signal_gate.store import Store

APPROVE_WORDS = [
    "y", "Y", "yes", "Yes", "yes please", "yep", "confirm",
    "do that", "go ahead", "correct", "  yes!  ", "Yes.", "yes, please",
]
REJECT_WORDS = [
    "no", "No", "cancel", "leave them", "keep the current goals",
    "don’t change them", "nope", "  no thanks ",
]
AMBIGUOUS_WORDS = ["maybe", "not sure", "hmm", "later"]
UNRELATED_WORDS = ["what's the weather like", "tell me a joke", "hello"]

CFG_BILL = "8051024863"


class Src:
    platform = "telegram"
    chat_type = "dm"

    def __init__(self):
        self.user_id = CFG_BILL
        self.chat_id = CFG_BILL


class Ev:
    def __init__(self, text, update_id="u1", message_id="m1"):
        self.text = text
        self.source = Src()
        self.platform_update_id = update_id
        self.message_id = message_id
        self.reply_to_message_id = None


class FakeAdapter:
    platform = "telegram"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None):
        self.sent.append(text)
        return type("R", (), {
            "success": True, "message_id": "1",
            "continuation_message_ids": (), "raw_response": {},
        })()


def _cfg(tmp_path):
    return {
        "enabled": True, "bill_user_id": CFG_BILL, "chat_id": CFG_BILL,
        "database": str(tmp_path / "sg.db"),
    }


def _install(monkeypatch, tmp_path, m3_response=None):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(service, "settings", lambda: cfg)

    async def fake_call(request, timeout=12):
        if m3_response is None:
            raise AssertionError("M3 must not be called")
        return m3_response

    monkeypatch.setattr(service.m3_client, "call", fake_call)
    return Store(cfg["database"])


def _pending(store, update_id="s1"):
    pid = store.prepare(
        bill_user_id=CFG_BILL, chat_id=CFG_BILL,
        source_update_id=update_id, source_message_id="sm1",
        display_sha256="d" * 64, proposal_sha256="p" * 64,
        proposal_json='{"actions":[{"text":"Finish Christine’s goals","mission":"billos"},'
                      '{"text":"Review SignalPath","mission":"signalpath"}],"no_today":null}',
    )
    store.arm(pid, "200")
    return pid


def _state(store, pid):
    with store.connect() as db:
        return db.execute(
            "SELECT state, error_code, error_detail FROM proposal WHERE id=?", (pid,)
        ).fetchone()


# ---------------- A. rendering: never raw JSON ----------------

@pytest.mark.parametrize("n", [1, 2, 3])
def test_render_human_has_no_raw_internals(n):
    actions = tuple({"text": f"Goal {i}", "mission": "billos"} for i in range(1, n + 1))
    text = schema.render_human(schema.Proposal(actions, replacement=True))
    for forbidden in ["{", "}", "```", "signal", "no_today", "mission",
                      "sha", "Reply Y", "reply with", "proposal_id"]:
        assert forbidden not in text
    assert "goal" in text.lower()
    assert text.count("\n1.") == 1  # numbered, human list


def test_render_human_two_goals_matches_required_shape():
    actions = (
        {"text": "Finish Christine’s goals", "mission": "billos"},
        {"text": "Review SignalPath", "mission": "signalpath"},
    )
    text = schema.render_human(schema.Proposal(actions, replacement=True))
    assert text == (
        "I have two goals for today:\n\n"
        "1. Finish Christine’s goals\n"
        "2. Review SignalPath\n\n"
        "Replace today’s current goals with these?"
    )


# ---------------- B/C. flexible confirmation ----------------

@pytest.mark.parametrize("word", APPROVE_WORDS)
def test_natural_approval_applies(tmp_path, monkeypatch, word):
    store = _install(monkeypatch, tmp_path, m3_response={
        "status": "APPLIED", "event_id": "EV1",
        "process_receipt": {"receipt_sha256": "a" * 64},
    })
    pid = _pending(store)
    adapter = FakeAdapter()
    handled = asyncio.run(service.intercept(adapter, Ev(word)))
    assert handled is True
    assert _state(store, pid)["state"] == "APPLIED"
    assert adapter.sent == ["Done. Today’s goals are updated."]


@pytest.mark.parametrize("word", REJECT_WORDS)
def test_natural_rejection_keeps_goals(tmp_path, monkeypatch, word):
    store = _install(monkeypatch, tmp_path, m3_response=None)  # M3 must NOT be called
    pid = _pending(store)
    adapter = FakeAdapter()
    handled = asyncio.run(service.intercept(adapter, Ev(word)))
    assert handled is True
    assert _state(store, pid)["state"] == "SUPERSEDED"
    assert adapter.sent == ["Kept your current goals. Nothing was changed."]


@pytest.mark.parametrize("word", AMBIGUOUS_WORDS)
def test_ambiguous_reply_clarifies_without_committing(tmp_path, monkeypatch, word):
    store = _install(monkeypatch, tmp_path, m3_response=None)
    pid = _pending(store)
    adapter = FakeAdapter()
    handled = asyncio.run(service.intercept(adapter, Ev(word)))
    assert handled is True
    assert _state(store, pid)["state"] == "PENDING"  # unchanged, still awaiting
    assert adapter.sent == [
        "Do you want me to replace today’s goals? Just say yes or no."
    ]


@pytest.mark.parametrize("word", UNRELATED_WORDS)
def test_unrelated_text_falls_through(tmp_path, monkeypatch, word):
    store = _install(monkeypatch, tmp_path, m3_response=None)
    pid = _pending(store)
    adapter = FakeAdapter()
    handled = asyncio.run(service.intercept(adapter, Ev(word)))
    assert handled is False               # ordinary conversation proceeds
    assert adapter.sent == []
    assert _state(store, pid)["state"] == "PENDING"


def test_normal_conversation_with_no_pending_proposal_falls_through(tmp_path, monkeypatch):
    store = _install(monkeypatch, tmp_path, m3_response=None)
    adapter = FakeAdapter()
    # Even a bare "yes" approves nothing when nothing is pending.
    assert asyncio.run(service.intercept(adapter, Ev("yes"))) is False
    assert adapter.sent == []


# ---------------- replay / idempotency ----------------

def test_repeated_approval_does_not_apply_twice(tmp_path, monkeypatch):
    calls = []

    store = _install(monkeypatch, tmp_path, m3_response={
        "status": "APPLIED", "event_id": "EV1",
        "process_receipt": {"receipt_sha256": "a" * 64},
    })
    # Count M3 calls.
    original = service.m3_client.call

    async def counting(request, timeout=12):
        calls.append(1)
        return await original(request, timeout=timeout)

    monkeypatch.setattr(service.m3_client, "call", counting)

    pid = _pending(store)
    adapter = FakeAdapter()
    asyncio.run(service.intercept(adapter, Ev("yes", update_id="u1")))
    second = asyncio.run(service.intercept(adapter, Ev("yes", update_id="u2")))

    assert len(calls) == 1                       # applied exactly once
    assert _state(store, pid)["state"] == "APPLIED"
    assert second is False                        # nothing left to approve


# ---------------- failed M3 hides detail, retains it locally ----------------

def test_failed_application_hides_detail_but_retains_locally(tmp_path, monkeypatch):
    store = _install(monkeypatch, tmp_path, m3_response={
        "status": "REJECTED", "applied": False,
        "error": {"code": "action_count", "message": "invalid action count"},
    })
    pid = _pending(store)
    adapter = FakeAdapter()
    asyncio.run(service.intercept(adapter, Ev("yes")))

    row = _state(store, pid)
    assert row["state"] == "FAILED"
    assert row["error_code"] == "action_count"          # retained locally
    assert row["error_detail"] == "invalid action count"
    assert adapter.sent == ["I couldn’t update today’s goals. Nothing was changed."]
    for leak in ["action_count", "invalid action count", "{", "sha"]:
        assert leak not in adapter.sent[0]


# ---------------- existing goals unchanged before confirmation ----------------

def test_pending_proposal_is_not_applied_before_confirmation(tmp_path, monkeypatch):
    store = _install(monkeypatch, tmp_path, m3_response=None)  # M3 must not be called
    pid = _pending(store)
    # No confirmation sent yet: proposal stays PENDING, M3 untouched.
    assert _state(store, pid)["state"] == "PENDING"

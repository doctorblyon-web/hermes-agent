"""Reminder orchestration: propose -> confirm -> M3-first apply, and its guards."""

import asyncio

from gateway.reminders import service
from tests.gateway.reminder_helpers import FakeBridge, FakeCron, FakeM3, cfg, event

run = asyncio.run


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "settings", lambda: cfg(tmp_path))
    return FakeM3(), FakeBridge(), FakeCron()


def _create(monkeypatch, tmp_path, m3, bridge, cron, *, text="remind me in two minutes to check Christine", update=100):
    return run(service.intercept(event(text, update=update), m3=m3, bridge=bridge, cron=cron))


def _confirm(m3, bridge, cron, *, update=101, text="yes"):
    return run(service.intercept(event(text, update=update), m3=m3, bridge=bridge, cron=cron))


def test_create_echoes_absolute_and_writes_nothing_canonical(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    reply = _create(monkeypatch, tmp_path, m3, bridge, cron)
    assert "check Christine" in reply
    assert "Reply yes to confirm" in reply
    assert "Australia/Sydney" in reply          # confirmation repeats the absolute time
    # Nothing canonical happened before confirmation.
    assert m3.reminders == {}
    assert m3.calls == []
    assert bridge.jobs == {}


def test_confirm_is_m3_first_then_one_linked_job_then_done(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    reply = _confirm(m3, bridge, cron)
    assert reply.startswith("Done.")
    # exactly one canonical reminder, scheduled, version 1
    assert len(m3.reminders) == 1
    rid, r = next(iter(m3.reminders.items()))
    assert r["status"] == "scheduled" and r["version"] == 1 and r["wording"] == "check Christine"
    # exactly one local job, linked to the canonical id
    assert bridge.jobs == {rid: "job1"}
    # M3 write happened before the local job existed: UPSERT precedes any job.
    ops = [c["operation"] for c in m3.calls]
    assert ops.count("REMINDER_UPSERT") == 1


def test_m3_failure_means_no_job_and_no_done(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    m3.fail = True
    reply = _confirm(m3, bridge, cron)
    assert "not claiming" in reply.lower()
    assert not reply.startswith("Done.")
    assert m3.reminders == {}       # nothing written
    assert bridge.jobs == {}         # no local job


def test_local_failure_after_m3_success_is_honest(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    bridge.fail_next = True           # M3 will succeed, local scheduling will fail
    reply = _confirm(m3, bridge, cron)
    assert not reply.startswith("Done.")
    assert "not claiming it's fully set" in reply.lower()
    # Canonical reminder exists (source of truth preserved for reconciliation)...
    assert len(m3.reminders) == 1
    # ...but no linked local job was claimed.
    assert bridge.jobs == {}


def test_duplicate_confirmation_is_rejected(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    first = _confirm(m3, bridge, cron, update=101)
    second = _confirm(m3, bridge, cron, update=101)   # same Telegram update replayed
    assert first.startswith("Done.")
    # The replay must never create a second canonical reminder or a second job,
    # whether it is answered "already handled" or falls through as ordinary text.
    assert second is None or "already handled" in second.lower()
    assert len(m3.reminders) == 1               # not written twice
    upserts = [c for c in m3.calls if c["operation"] == "REMINDER_UPSERT"]
    assert len(upserts) == 1
    assert len(bridge.jobs) == 1


def test_reject_writes_nothing(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    reply = _confirm(m3, bridge, cron, text="no")
    assert "won't" in reply.lower() or "nothing was changed" in reply.lower()
    assert m3.reminders == {}
    assert bridge.jobs == {}


def test_ambiguous_reply_keeps_proposal_pending(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _create(monkeypatch, tmp_path, m3, bridge, cron)
    maybe = _confirm(m3, bridge, cron, text="maybe", update=101)
    assert "yes or no" in maybe.lower()
    assert m3.reminders == {}
    # A subsequent clear yes still works.
    done = _confirm(m3, bridge, cron, text="yes", update=102)
    assert done.startswith("Done.")
    assert len(m3.reminders) == 1


def test_wrong_lane_is_ignored(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    reply = run(service.intercept(
        event("remind me in two minutes to check Christine", user_id="999"),
        m3=m3, bridge=bridge, cron=cron))
    assert reply is None


def test_ordinary_conversation_falls_through(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    for text in ["how are you today?", "what's the weather like", "thanks Christine"]:
        assert run(service.intercept(event(text), m3=m3, bridge=bridge, cron=cron)) is None
    assert m3.reminders == {}


def test_bare_yes_with_no_pending_proposal_falls_through(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    assert run(service.intercept(event("yes"), m3=m3, bridge=bridge, cron=cron)) is None


def test_missing_time_asks_when(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    reply = _create(monkeypatch, tmp_path, m3, bridge, cron, text="remind me to call mum tomorrow")
    assert reply is not None and "when" in reply.lower()
    assert m3.reminders == {}

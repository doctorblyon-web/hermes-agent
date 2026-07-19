"""Conversational management: view, change, snooze, cancel — each M3-first."""

import asyncio

from gateway.reminders import service
from tests.gateway.reminder_helpers import FakeBridge, FakeCron, FakeM3, cfg, event

run = asyncio.run
RID = "rem_" + "a" * 24
RID2 = "rem_" + "b" * 24


def _wire(monkeypatch, tmp_path, seed=None):
    monkeypatch.setattr(service, "settings", lambda: cfg(tmp_path))
    m3, bridge, cron = FakeM3(), FakeBridge(), FakeCron()
    for rid, kw in (seed or {}).items():
        m3.seed(rid, **kw)
    return m3, bridge, cron


def _say(m3, bridge, cron, text, update):
    return run(service.intercept(event(text, update=update), m3=m3, bridge=bridge, cron=cron))


def test_view_lists_scheduled_reminders(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={
        RID: dict(wording="review SignalPath"),
        RID2: dict(wording="call the accountant", due_at="2026-07-21T04:00:00Z"),
    })
    reply = _say(m3, bridge, cron, "what reminders do I have?", 1)
    assert "review SignalPath" in reply and "call the accountant" in reply
    assert "Monday 20 July 2026" in reply


def test_view_with_none(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path)
    assert "no reminders" in _say(m3, bridge, cron, "show me my reminders", 1).lower()


def test_cancel_flow(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={RID: dict(wording="review SignalPath")})
    proposal = _say(m3, bridge, cron, "cancel the reminder to review SignalPath", 1)
    assert "Cancel the reminder" in proposal and "review SignalPath" in proposal
    assert m3.reminders[RID]["status"] == "scheduled"      # not cancelled before confirm
    done = _say(m3, bridge, cron, "yes", 2)
    assert done.startswith("Done.")
    assert m3.reminders[RID]["status"] == "cancelled"
    cancels = [c for c in m3.calls if c.get("operation") == "REMINDER_UPSERT" and c.get("action") == "cancel"]
    assert len(cancels) == 1 and cancels[0]["reminder"]["base_version"] == 1


def test_change_flow_bumps_version_and_replaces_job(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={RID: dict(wording="review SignalPath")})
    proposal = _say(m3, bridge, cron, "change the SignalPath reminder to 10am", 1)
    assert "Change the reminder" in proposal and "10:00 AM" in proposal
    assert m3.reminders[RID]["version"] == 1               # unchanged before confirm
    done = _say(m3, bridge, cron, "yes", 2)
    assert done.startswith("Done.")
    assert m3.reminders[RID]["version"] == 2
    assert m3.reminders[RID]["status"] == "scheduled"
    assert list(bridge.jobs) == [RID]                      # exactly one linked job


def test_snooze_flow(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={RID: dict(wording="review SignalPath")})
    proposal = _say(m3, bridge, cron, "snooze it for 10 minutes", 1)
    assert "Snooze the reminder" in proposal
    done = _say(m3, bridge, cron, "yes", 2)
    assert done.startswith("Done.")
    assert m3.reminders[RID]["version"] == 2
    snoozes = [c for c in m3.calls if c.get("operation") == "REMINDER_UPSERT" and c.get("action") == "snooze"]
    assert len(snoozes) == 1


def test_cancel_unknown_reminder_is_reported(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={RID: dict(wording="review SignalPath")})
    reply = _say(m3, bridge, cron, "cancel the reminder about the dentist", 1)
    assert "couldn't find" in reply.lower()
    assert m3.reminders[RID]["status"] == "scheduled"


def test_change_ambiguous_time_asks(monkeypatch, tmp_path):
    m3, bridge, cron = _wire(monkeypatch, tmp_path, seed={RID: dict(wording="review SignalPath")})
    reply = _say(m3, bridge, cron, "change the SignalPath reminder to 9 o'clock", 1)
    assert "morning or the evening" in reply.lower()
    assert m3.reminders[RID]["version"] == 1

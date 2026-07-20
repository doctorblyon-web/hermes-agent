"""PA object service pipeline against the in-memory M3 spec.

Covers: obligation creation, needs_bill creation, idempotent replay (no
duplicate), permitted status transitions, rejected invalid transitions, absence
of any deletion path, verbatim wording preservation, no invented fields, and
ordinary-conversation fall-through.
"""

import asyncio

import pytest

from gateway.pa_objects import service
from tests.gateway.pa_object_helpers import FakeM3, cfg, event

run = asyncio.run


@pytest.fixture
def wired(tmp_path, monkeypatch):
    conf = cfg(tmp_path)
    monkeypatch.setattr(service, "settings", lambda: conf)
    return FakeM3()


def test_gate_disabled_falls_through(tmp_path, monkeypatch):
    conf = cfg(tmp_path, enabled=False)
    monkeypatch.setattr(service, "settings", lambda: conf)
    m3 = FakeM3()
    assert run(service.intercept(event("I need to send Gina the material by Friday"), m3=m3)) is None
    assert not m3.records


def test_obligation_creation(wired):
    m3 = wired
    reply = run(service.intercept(event("I need to send Gina the revised material by Friday.", update=1), m3=m3))
    assert reply is not None and "obligation" in reply.lower()
    assert len(m3.records) == 1
    body = next(iter(m3.records.values()))[-1]
    assert body["kind"] == "obligation" and body["status"] == "open"
    assert body["wording"] == "send Gina the revised material by Friday."   # verbatim
    assert body["person"] == "Gina" and body["due_date"] is not None


def test_needs_bill_creation(wired):
    m3 = wired
    reply = run(service.intercept(event("I need to decide whether to start the Composio pilot.", update=2), m3=m3))
    assert reply is not None and "decision" in reply.lower()
    body = next(iter(m3.records.values()))[-1]
    assert body["kind"] == "needs_bill" and body["status"] == "open"
    assert body["wording"] == "decide whether to start the Composio pilot."
    assert body["decision"] is None                                          # not invented


def test_bare_obligation_has_no_invented_fields(wired):
    m3 = wired
    run(service.intercept(event("I have to call the accountant", update=3), m3=m3))
    body = next(iter(m3.records.values()))[-1]
    assert body["person"] is None and body["due_date"] is None and body["project"] is None


def test_replay_does_not_duplicate(wired):
    m3 = wired
    ev = event("I need to send Gina the revised material by Friday.", update=7)
    first = run(service.intercept(ev, m3=m3))
    upserts_after_first = sum(1 for c in m3.calls if c.get("operation") == "PA_OBJECT_UPSERT")
    second = run(service.intercept(ev, m3=m3))   # same Telegram update replayed
    assert first == second
    assert len(m3.records) == 1                  # exactly one canonical object
    upserts_after_second = sum(1 for c in m3.calls if c.get("operation") == "PA_OBJECT_UPSERT")
    assert upserts_after_second == upserts_after_first  # replay made no new M3 write


def test_permitted_transitions_full_lifecycle(wired):
    m3 = wired
    run(service.intercept(event("I need to send Gina the revised material by Friday.", update=10), m3=m3))
    oid = next(iter(m3.records))

    r = run(service.intercept(event("I'm waiting for Gina to confirm the final changes.", update=11), m3=m3))
    assert "waiting" in r.lower()
    assert m3.records[oid][-1]["status"] == "waiting"

    r = run(service.intercept(event("Gina confirmed the changes; resume that obligation.", update=12), m3=m3))
    assert m3.records[oid][-1]["status"] == "open"

    r = run(service.intercept(event("Mark the Gina obligation complete.", update=13), m3=m3))
    assert "complete" in r.lower()
    assert m3.records[oid][-1]["status"] == "completed"
    # versions accumulate; nothing is ever removed
    assert [v["version"] for v in m3.records[oid]] == [1, 2, 3, 4]


def test_needs_bill_resolve(wired):
    m3 = wired
    run(service.intercept(event("I need to decide whether to start the Composio pilot.", update=20), m3=m3))
    oid = next(iter(m3.records))
    r = run(service.intercept(event("Resolve that decision: do not start the pilot yet.", update=21), m3=m3))
    assert "resolved" in r.lower()
    head = m3.records[oid][-1]
    assert head["status"] == "resolved" and head["decision"] == "do not start the pilot yet."


def test_invalid_transition_is_refused_not_applied(wired):
    m3 = wired
    # create an OPEN obligation, then try to resume it (resume is waiting-only)
    run(service.intercept(event("I need to send Gina the material by Friday.", update=30), m3=m3))
    oid = next(iter(m3.records))
    r = run(service.intercept(event("resume that obligation", update=31), m3=m3))
    assert "couldn't find" in r.lower()
    # unchanged: still open, still one version
    assert m3.records[oid][-1]["status"] == "open"
    assert len(m3.records[oid]) == 1


def test_transition_with_no_matching_object(wired):
    m3 = wired
    r = run(service.intercept(event("Mark the Gina obligation complete.", update=40), m3=m3))
    assert "couldn't find" in r.lower()
    assert not m3.records


def test_completed_object_cannot_be_transitioned_again(wired):
    m3 = wired
    run(service.intercept(event("I need to send Gina the material by Friday.", update=50), m3=m3))
    run(service.intercept(event("Mark the Gina obligation complete.", update=51), m3=m3))
    oid = next(iter(m3.records))
    assert m3.records[oid][-1]["status"] == "completed"
    # any further transition attempt finds no eligible target -> nothing changes
    r = run(service.intercept(event("Cancel that obligation; no longer required.", update=52), m3=m3))
    assert "couldn't find" in r.lower()
    assert m3.records[oid][-1]["status"] == "completed"


def test_no_deletion_path(wired):
    m3 = wired
    run(service.intercept(event("I need to send Gina the material by Friday.", update=60), m3=m3))
    oid = next(iter(m3.records))
    for text in ["delete that obligation", "remove the Gina obligation", "erase that obligation"]:
        assert run(service.intercept(event(text, update=hash(text) % 100000), m3=m3)) is None
    assert oid in m3.records and m3.records[oid][-1]["status"] == "open"


def test_wording_preserved_end_to_end(wired):
    m3 = wired
    odd = "I need to e-mail Dr. O'Brien re: the Q3 numbers by Monday"
    run(service.intercept(event(odd, update=70), m3=m3))
    body = next(iter(m3.records.values()))[-1]
    assert body["wording"] == "e-mail Dr. O'Brien re: the Q3 numbers by Monday"


def test_ordinary_conversation_falls_through(wired):
    m3 = wired
    for i, text in enumerate(["How are you?", "yes", "I think that's fine", "thanks!"]):
        assert run(service.intercept(event(text, update=80 + i), m3=m3)) is None
    assert not m3.records

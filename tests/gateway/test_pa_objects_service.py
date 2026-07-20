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


# =============================================================================
# Model-facing tool API (tool_create / tool_transition). These are what the
# `pa_object` model tool calls AFTER Christine's model has understood the
# request through conversation. Structured arguments only; no NL interpretation.
# =============================================================================

def test_tool_create_obligation_resolves_natural_due(wired):
    m3 = wired
    r = run(service.tool_create(
        "obligation", "send Gina the revised material by Friday",
        person="Gina", due="Friday", session_id="s1", m3=m3))
    assert r["ok"] is True and "obligation" in r["message"].lower()
    body = next(iter(m3.records.values()))[-1]
    assert body["kind"] == "obligation" and body["status"] == "open"
    assert body["wording"] == "send Gina the revised material by Friday"  # verbatim
    assert body["person"] == "Gina" and body["due_date"] is not None       # resolved, not invented


def test_tool_create_needs_bill(wired):
    m3 = wired
    r = run(service.tool_create(
        "needs_bill", "whether to start the Composio pilot", session_id="s2", m3=m3))
    assert r["ok"] is True and "decision" in r["message"].lower()
    body = next(iter(m3.records.values()))[-1]
    assert body["kind"] == "needs_bill" and body["decision"] is None       # not invented


def test_tool_create_bare_obligation_invents_nothing(wired):
    m3 = wired
    r = run(service.tool_create("obligation", "call the accountant", session_id="s3", m3=m3))
    assert r["ok"] is True
    body = next(iter(m3.records.values()))[-1]
    assert body["person"] is None and body["due_date"] is None and body["project"] is None


def test_tool_create_is_idempotent_within_session(wired):
    m3 = wired
    a = run(service.tool_create("obligation", "email Sam the deck", session_id="dup", m3=m3))
    upserts_a = sum(1 for c in m3.calls if c.get("operation") == "PA_OBJECT_UPSERT")
    b = run(service.tool_create("obligation", "email Sam the deck", session_id="dup", m3=m3))
    upserts_b = sum(1 for c in m3.calls if c.get("operation") == "PA_OBJECT_UPSERT")
    assert a["object_id"] == b["object_id"]
    assert b.get("already") is True
    assert len(m3.records) == 1                 # exactly one canonical object
    assert upserts_b == upserts_a               # replay made no new canonical write


def test_tool_transition_complete_by_reference(wired):
    m3 = wired
    run(service.tool_create("obligation", "send Gina the material", person="Gina", session_id="c", m3=m3))
    oid = next(iter(m3.records))
    r = run(service.tool_transition("obligation", "complete", reference="Gina", session_id="t", m3=m3))
    assert r["ok"] is True and m3.records[oid][-1]["status"] == "completed"


def test_tool_transition_resolve_requires_decision_and_preserves_it(wired):
    m3 = wired
    run(service.tool_create("needs_bill", "whether to start the pilot", session_id="c", m3=m3))
    # no decision -> refused, nothing changed
    r0 = run(service.tool_transition("needs_bill", "resolve", reference="pilot", session_id="t0", m3=m3))
    assert r0["ok"] is False
    oid = next(iter(m3.records))
    assert m3.records[oid][-1]["status"] == "open"
    # with a verbatim decision -> resolved, decision preserved
    r1 = run(service.tool_transition(
        "needs_bill", "resolve", reference="pilot",
        decision="do not start it yet", session_id="t1", m3=m3))
    assert r1["ok"] is True
    head = m3.records[oid][-1]
    assert head["status"] == "resolved" and head["decision"] == "do not start it yet"


def test_tool_transition_ambiguous_reference_asks_not_writes(wired):
    m3 = wired
    run(service.tool_create("obligation", "call the plumber", session_id="a", m3=m3))
    run(service.tool_create("obligation", "call the electrician", session_id="b", m3=m3))
    r = run(service.tool_transition("obligation", "complete", reference="call", session_id="t", m3=m3))
    assert r["ok"] is False and ("which" in r["message"].lower() or "more than one" in r["message"].lower())
    # both still open — nothing was changed on an ambiguous target
    assert all(v[-1]["status"] == "open" for v in m3.records.values())


def test_tool_transition_unknown_reference_changes_nothing(wired):
    m3 = wired
    run(service.tool_create("obligation", "water the plants", session_id="a", m3=m3))
    r = run(service.tool_transition("obligation", "complete", reference="taxes", session_id="t", m3=m3))
    assert r["ok"] is False
    assert all(v[-1]["status"] == "open" for v in m3.records.values())


def test_tool_create_gate_disabled_refuses(tmp_path, monkeypatch):
    from tests.gateway.pa_object_helpers import cfg as _cfg
    monkeypatch.setattr(service, "settings", lambda: _cfg(tmp_path, enabled=False))
    m3 = FakeM3()
    r = run(service.tool_create("obligation", "do a thing", session_id="s", m3=m3))
    assert r["ok"] is False and not m3.records

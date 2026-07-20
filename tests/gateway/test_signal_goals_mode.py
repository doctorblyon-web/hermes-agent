"""Explicit Goals conversation-state gate.

Goals mode is entered only by an explicit opener or by answering Christine's
"what are your goals?" question — never by ordinary "I need to…" / "I've got
to…" / "I need to remember…" / "make sure I…" statements. It ends after
confirmation, rejection, cancellation or timeout.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.signal_gate import goals_mode, service

run = asyncio.run
BILL = "8051024863"


def _cfg(tmp_path, **over):
    base = {"enabled": True, "bill_user_id": BILL, "chat_id": BILL,
            "database": str(tmp_path / "sg.db"),
            "goals_mode_db": str(tmp_path / "goals_mode.sqlite")}
    base.update(over)
    return base


def _event(text, update="u1", message="m1"):
    source = SimpleNamespace(platform="telegram", chat_type="dm",
                             user_id=BILL, chat_id=BILL)
    return SimpleNamespace(text=text, source=source,
                           platform_update_id=update, message_id=message)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    conf = _cfg(tmp_path)
    monkeypatch.setattr(service, "settings", lambda: conf)
    monkeypatch.setattr(service, "_today_state", lambda: {
        "target_date": "2026-07-20", "has_goals": False, "state_sha256": "0" * 64})

    async def _fake_capture(event, text, **kw):
        return SimpleNamespace(
            capture_id="cap_" + "a" * 32,
            event_id="RAW_CAPTURE_APPEND-20260720T000000000000Z-bill",
            content_sha256="a" * 64, receipt_sha256="b" * 64,
            source_update_id=str(event.platform_update_id),
            source_message_id=str(event.message_id))

    import gateway.raw_capture_gate.service as rc
    monkeypatch.setattr(rc, "capture_result", _fake_capture)
    return conf


# ---- recognition units ------------------------------------------------------

def test_detect_initiation_inline_and_ask():
    inline = service._detect_initiation("These are my goals for today: 1) Ship X 2) Call Y")
    assert inline[0] == "inline" and inline[1] == ("Ship X", "Call Y")
    assert service._detect_initiation("Let's do today's goals.") == ("ask",)
    assert service._detect_initiation("Set my goals for today") == ("ask",)
    assert service._detect_initiation("Today's goals are finish the deck and call Sam")[0] == "inline"


def test_detect_initiation_ignores_ordinary_messages():
    for text in [
        "I need to send Gina the material by Friday",
        "I've got to call the accountant",
        "Christine, I need to remember to organize a talk for the meeting tomorrow night",
        "make sure I email Sam",
        "I need to decide whether to start the pilot",
        "How are you today?",
    ]:
        assert service._detect_initiation(text) is None, text


def test_looks_like_other_intent():
    assert service._looks_like_other_intent("I need to remember to organize a talk")
    assert service._looks_like_other_intent("Christine, I need to remember to book a room")
    assert service._looks_like_other_intent("remind me to call Sam at 5")
    assert service._looks_like_other_intent("make sure I reply to Gina")
    assert not service._looks_like_other_intent("Ship the release")
    assert not service._looks_like_other_intent("Finish the deck")


def test_parse_goal_lines_forms():
    assert service._parse_goal_lines("1) A\n2) B\n3) C") == ("A", "B", "C")
    assert service._parse_goal_lines("A, B and C") == ("A", "B", "C")
    assert service._parse_goal_lines("- Ship it") == ("Ship it",)


# ---- goals_mode state -------------------------------------------------------

def test_goals_mode_arm_and_timeout(tmp_path):
    conf = _cfg(tmp_path)
    src = _event("x").source
    assert goals_mode.is_armed(conf, src) is False
    goals_mode.arm(conf, src, ttl=1000)
    assert goals_mode.is_armed(conf, src) is True
    # expired
    assert goals_mode.is_armed(conf, src, now=__import__("time").time() + 2000) is False
    # and pruned
    assert goals_mode.is_armed(conf, src) is False


def test_goals_mode_disarm(tmp_path):
    conf = _cfg(tmp_path)
    src = _event("x").source
    goals_mode.arm(conf, src)
    goals_mode.disarm(conf, src)
    assert goals_mode.is_armed(conf, src) is False


# ---- behaviour through intercept_today_goals --------------------------------

def test_explicit_inline_opener_enters_goals(wired):
    reply = run(service.intercept_today_goals(_event("These are my goals for today: 1) Ship X 2) Call Y")))
    assert reply is not None
    assert "Ship X" in reply and "Call Y" in reply          # a goals proposal was produced


def test_opener_without_goals_asks_and_arms(wired):
    conf = wired
    reply = run(service.intercept_today_goals(_event("Let's do today's goals.")))
    assert reply is not None and "what are your top three goals" in reply.lower()
    assert goals_mode.is_armed(conf, _event("x").source) is True


def test_armed_next_reply_accepted_as_goals(wired):
    conf = wired
    run(service.intercept_today_goals(_event("Set my goals for today", update="u1")))
    assert goals_mode.is_armed(conf, _event("x").source) is True
    reply = run(service.intercept_today_goals(_event("1) Ship the release\n2) Review SignalPath", update="u2")))
    assert reply is not None
    assert "Ship the release" in reply and "Review SignalPath" in reply
    # window consumed
    assert goals_mode.is_armed(conf, _event("x").source) is False


def test_i_need_to_remember_outside_goals_does_not_enter(wired):
    reply = run(service.intercept_today_goals(
        _event("Christine, I need to remember to organize a talk for the meeting tomorrow night")))
    assert reply is None                                    # falls through to obligation routing


def test_armed_but_obligation_reply_falls_through(wired):
    conf = wired
    run(service.intercept_today_goals(_event("Let's do today's goals.", update="u1")))
    assert goals_mode.is_armed(conf, _event("x").source) is True
    # even while armed, a clear obligation is NOT captured as a goal…
    reply = run(service.intercept_today_goals(
        _event("I need to remember to organize a talk for the meeting tomorrow night", update="u2")))
    assert reply is None
    # …and Goals mode has ended (window consumed)
    assert goals_mode.is_armed(conf, _event("x").source) is False


def test_ordinary_message_outside_goals_is_ignored(wired):
    assert run(service.intercept_today_goals(_event("How are you today?"))) is None
    assert run(service.intercept_today_goals(_event("I've got to call the accountant"))) is None

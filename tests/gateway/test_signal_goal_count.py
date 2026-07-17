"""Focused tests for the one-to-three goals correction.

The user-facing "always three" behaviour lived in the Christine persona prompt
(SOUL.md, live content) and was reinforced by the model-generated ```signal
envelope schema requiring exactly three actions. This suite locks the code-side
invariant: the SIGNAL envelope now accepts one, two, or three actions and still
rejects zero or four, wording is preserved, and presenting a proposal never
applies anything before the deterministic Y confirmation. (The natural-language
interpretation and "never ask for a third" behaviour are governed by SOUL.md and
verified by the live canary.)
"""

import asyncio

import pytest

from gateway.signal_gate import schema, service


def _proposal(n, today_goals=False):
    actions = tuple(
        {"text": f"Goal {i}", "mission": "billos"} for i in range(1, n + 1)
    )
    return schema.Proposal(actions, today_goals=today_goals)


# ---- ```signal JSON envelope (the format SOUL instructs the model to emit) ----

@pytest.mark.parametrize("n", [1, 2, 3])
def test_json_envelope_accepts_one_to_three(n):
    parsed = schema.parse_display(schema.render_display(_proposal(n)))
    assert parsed is not None
    assert len(parsed.actions) == n


def test_json_envelope_rejects_zero():
    with pytest.raises(schema.ValidationError) as exc:
        schema.parse_display(schema.render_display(_proposal(0)))
    assert exc.value.code == "action_count"


def test_json_envelope_rejects_four():
    with pytest.raises(schema.ValidationError) as exc:
        schema.parse_display(schema.render_display(_proposal(4)))
    assert exc.value.code == "action_count"


# ---- human "I've prepared these as today's goals:" envelope (numbered) ----

@pytest.mark.parametrize("n", [1, 2, 3])
def test_today_goals_numbered_envelope_accepts_one_to_three(n):
    parsed = schema.parse_display(schema.render_display(_proposal(n, today_goals=True)))
    assert parsed is not None
    assert parsed.today_goals is True
    assert len(parsed.actions) == n


def test_today_goals_numbered_envelope_rejects_four():
    with pytest.raises(schema.ValidationError) as exc:
        schema.parse_display(schema.render_display(_proposal(4, today_goals=True)))
    assert exc.value.code == "action_count"


# ---- wording preservation (the exact two-goal canary content) ----

def test_two_goal_wording_preserved_round_trip():
    actions = (
        {"text": "Finish Christine’s goals", "mission": "billos"},
        {"text": "Review SignalPath", "mission": "signalpath"},
    )
    parsed = schema.parse_display(schema.render_display(schema.Proposal(actions)))
    assert [a["text"] for a in parsed.actions] == [
        "Finish Christine’s goals", "Review SignalPath",
    ]
    assert [a["mission"] for a in parsed.actions] == ["billos", "signalpath"]


# ---- presenting a proposal must not apply anything before confirmation ----

def test_parsing_or_rendering_a_two_goal_proposal_does_not_touch_m3(monkeypatch):
    called = []

    async def boom(*args, **kwargs):
        called.append(1)
        raise AssertionError("M3 must not be called before confirmation")

    monkeypatch.setattr(service.m3_client, "call", boom)
    schema.parse_display(schema.render_display(_proposal(2)))
    assert not called


# ---- ordinary non-goal conversation is not intercepted as a goals command ----

class _Ev:
    def __init__(self, text):
        self.text = text


@pytest.mark.parametrize("text", [
    "what's the weather like today",
    "can you help me draft an email",
    "today was a good day",
])
def test_ordinary_conversation_falls_through(text):
    assert asyncio.run(service.intercept_today_goals(_Ev(text))) is None

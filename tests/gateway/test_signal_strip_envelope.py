"""strip_signal_envelope: a rejected SIGNAL envelope must never swallow the reply.

When deterministic validation rejects a SIGNAL envelope, the delivery path salvages
the model's surrounding conversational content (a reflected obligation, a separate
request) instead of replacing the whole message with an error line. These tests pin
that salvage behaviour.
"""

from gateway.signal_gate.service import strip_signal_envelope as strip


def test_prose_around_bad_block_survives():
    text = (
        "Adelaide — happy to help get tickets. Which dates are you flying?\n\n"
        "Captured.\n```signal\n{malformed for tomorrow}\n```\n"
        "Captured as a proposal. Reply Y within 30 minutes to approve canonical application."
    )
    out = strip(text)
    assert "Adelaide" in out and "Which dates" in out
    assert "```signal" not in out
    assert "approve canonical application" not in out.lower()
    assert "Captured." not in out


def test_pure_envelope_reduces_to_empty():
    text = (
        "Captured.\n```signal\n{malformed}\n```\n"
        "Captured as a proposal. Reply Y within 30 minutes to approve canonical application."
    )
    assert strip(text) == ""


def test_goals_style_prefix_stripped_content_kept():
    text = (
        "I’ve prepared these as today’s goals:\n\n1. one\n2. two\n\n"
        "Reply Y within 30 minutes to approve canonical application."
    )
    out = strip(text)
    assert "I’ve prepared these as today’s goals" not in out
    assert "one" in out and "two" in out


def test_ordinary_reply_untouched():
    assert strip("Just a normal reply, nothing to see here.") == "Just a normal reply, nothing to see here."


def test_non_string_is_safe():
    assert strip(None) == ""
    assert strip("") == ""

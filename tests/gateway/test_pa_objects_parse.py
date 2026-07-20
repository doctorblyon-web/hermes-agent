"""PA object recognition: the four canaries, fall-through, date, no invention."""

from datetime import datetime

from gateway.pa_objects import parse


# --- creation ----------------------------------------------------------------

def test_canary_obligation_creation():
    c = parse.parse_create("I need to send Gina the revised material by Friday.")
    assert c is not None and c.kind == "obligation"
    assert c.wording == "send Gina the revised material by Friday."
    assert c.person == "Gina"
    assert c.due_date is not None
    assert datetime.strptime(c.due_date, "%Y-%m-%d").strftime("%A") == "Friday"


def test_canary_needs_bill_creation():
    c = parse.parse_create("I need to decide whether to start the Composio pilot.")
    assert c is not None and c.kind == "needs_bill"
    assert c.wording == "decide whether to start the Composio pilot."
    assert c.person is None and c.due_date is None and c.project is None


def test_obligation_no_invented_fields():
    c = parse.parse_create("I have to call the accountant")
    assert c.kind == "obligation" and c.wording == "call the accountant"
    assert c.person is None and c.due_date is None and c.project is None


def test_obligation_project_only_when_explicit():
    c = parse.parse_create("I need to draft the brief for the SignalPath project by tomorrow")
    assert c.project == "SignalPath"
    assert c.due_date is not None


def test_ordinary_conversation_falls_through():
    for text in [
        "How are you today?",
        "I think we should ship it",
        "I need coffee",              # no "to <verb>"
        "yes",
        "Thanks, that's great",
        "Can you summarise this thread?",
    ]:
        assert parse.parse_create(text) is None, text
        assert parse.parse_transition(text) is None, text


# --- transitions -------------------------------------------------------------

def test_canary_waiting_transition():
    t = parse.parse_transition("I'm waiting for Gina to confirm the final changes.")
    assert t is not None and t.kind == "obligation" and t.action == "set_waiting"
    assert t.ref == "Gina"


def test_resume_transition():
    t = parse.parse_transition("Gina confirmed the changes; resume that obligation.")
    assert t is not None and t.action == "resume"


def test_complete_transition():
    t = parse.parse_transition("Mark the Gina obligation complete.")
    assert t is not None and t.action == "complete" and t.ref == "Gina"


def test_cancel_obligation_transition():
    t = parse.parse_transition("Cancel that obligation; it is no longer required.")
    assert t is not None and t.kind == "obligation" and t.action == "cancel"


def test_canary_resolve_decision():
    t = parse.parse_transition("Resolve that decision: do not start the pilot yet.")
    assert t is not None and t.kind == "needs_bill" and t.action == "resolve"
    assert t.decision == "do not start the pilot yet."


def test_cancel_decision_transition():
    t = parse.parse_transition("Cancel that decision.")
    assert t is not None and t.kind == "needs_bill" and t.action == "cancel"


def test_no_delete_verb_recognised():
    for text in ["delete that obligation", "remove the Gina obligation",
                 "erase that decision", "purge my obligations"]:
        assert parse.parse_transition(text) is None, text


def test_resolve_requires_a_decision_body():
    # "resolve that decision" with no decision text is not a valid resolve
    assert parse.parse_transition("resolve that decision") is None


def test_leading_address_is_stripped_not_part_of_wording():
    c = parse.parse_create("Christine, I need to call the accountant")
    assert c is not None and c.kind == "obligation"
    assert c.wording == "call the accountant"          # "Christine," is address, not wording


def test_point4_sentence_is_obligation_due_tomorrow_not_goals():
    s = "Christine, I need to remember to organize a talk for the meeting tomorrow night"
    c = parse.parse_create(s)
    assert c is not None and c.kind == "obligation"
    assert c.wording == "remember to organize a talk for the meeting tomorrow night"
    assert c.due_date is not None                       # "tomorrow night" resolved to a date


def test_trailing_day_expression_sets_due():
    base = datetime(2026, 7, 20, 9, 0)                  # Monday
    assert parse.parse_create("I need to draft the deck tomorrow", now=base).due_date == "2026-07-21"
    assert parse.parse_create("I have to call Sam on Friday", now=base).due_date == "2026-07-24"


def test_date_independent_of_host_zone():
    base = datetime(2026, 7, 20, 9, 0)   # a Monday
    d = parse.resolve_due_date("Friday", now=base)
    assert d.isoformat() == "2026-07-24"

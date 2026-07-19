"""Reminder payload validation and per-action object shapes (M3 wire terms)."""

import pytest

from gateway.reminders import schema

PROV = {"telegram_update_id": "1", "telegram_user_id": "2",
        "telegram_chat_id": "3", "source_message_id": "4"}


def test_clean_text_normalises_and_collapses():
    assert schema.clean_text("  review   SignalPath \n now ") == "review SignalPath now"


def test_clean_text_rejects_empty_and_control():
    with pytest.raises(schema.ValidationError):
        schema.clean_text("   ")
    with pytest.raises(schema.ValidationError):
        schema.clean_text("bad\x00text")


def test_create_object_is_exact_schema():
    p = schema.ReminderProposal(action="create", wording="do it",
                                due_at="2026-07-20T09:00:00+10:00", recurrence=None, provenance=PROV)
    obj = p.reminder_object()
    assert set(obj) == {"wording", "content_sha256", "due_at", "timezone", "recurrence", "provenance"}
    assert obj["content_sha256"] == schema.sha256("do it")
    assert obj["provenance"] == PROV


def test_cancel_object_is_exact_schema():
    p = schema.ReminderProposal(action="cancel", wording="do it", due_at=None,
                                reminder_id="rem_" + "a" * 24, base_version=2)
    assert set(p.reminder_object()) == {"reminder_id", "base_version"}


def test_snooze_object_is_exact_schema():
    p = schema.ReminderProposal(action="snooze", wording="do it",
                                due_at="2026-07-20T09:00:00+10:00",
                                reminder_id="rem_" + "a" * 24, base_version=2)
    assert set(p.reminder_object()) == {"reminder_id", "base_version", "due_at"}


def test_change_object_is_exact_schema():
    p = schema.ReminderProposal(action="change", wording="new",
                                due_at="2026-07-20T09:00:00+10:00", recurrence=None,
                                reminder_id="rem_" + "a" * 24, base_version=2)
    obj = p.reminder_object()
    assert set(obj) == {"reminder_id", "base_version", "wording", "content_sha256",
                        "due_at", "timezone", "recurrence"}
    assert obj["content_sha256"] == schema.sha256("new")


def test_content_hash_matches_m3_definition():
    # M3 requires content_sha256 == sha256(wording); Williams computes identically.
    p = schema.ReminderProposal(action="create", wording="hello world",
                                due_at="2026-07-20T09:00:00+10:00", provenance=PROV)
    assert p.content_sha256() == schema.sha256("hello world")


def test_validate_action():
    assert schema.validate_action("snooze") == "snooze"
    with pytest.raises(schema.ValidationError):
        schema.validate_action("explode")

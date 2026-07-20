"""PA object schema: verbatim wording preservation, transition tables, projection."""

import pytest

from gateway.pa_objects import schema


def test_wording_preserved_verbatim():
    raw = "  Send Gina the REVISED material, by Friday.  "
    assert schema.preserve_wording(raw) == "Send Gina the REVISED material, by Friday."


def test_wording_never_normalises_internal_text():
    raw = "reply to  Dr.  O'Brien   re: the  contract"
    # internal spacing / punctuation / casing are left exactly as written
    assert schema.preserve_wording(raw) == raw.strip()


def test_wording_rejects_empty_and_control():
    with pytest.raises(schema.ValidationError):
        schema.preserve_wording("   ")
    with pytest.raises(schema.ValidationError):
        schema.preserve_wording("bad\x00text")


def test_transition_tables_match_contract():
    assert schema.TRANSITIONS["obligation"]["set_waiting"] == (("open",), "waiting")
    assert schema.TRANSITIONS["obligation"]["resume"] == (("waiting",), "open")
    assert schema.TRANSITIONS["obligation"]["complete"] == (("open", "waiting"), "completed")
    assert schema.TRANSITIONS["obligation"]["cancel"] == (("open", "waiting"), "cancelled")
    assert schema.TRANSITIONS["needs_bill"]["resolve"] == (("open",), "resolved")
    assert schema.TRANSITIONS["needs_bill"]["cancel"] == (("open",), "cancelled")


def test_no_delete_or_reopen_transition_exists():
    for kind in schema.KINDS:
        assert "delete" not in schema.TRANSITIONS[kind]
        assert "purge" not in schema.TRANSITIONS[kind]
    # a completed/cancelled/resolved object has no outward transition at all
    terminal = {"completed", "cancelled", "resolved"}
    for kind, table in schema.TRANSITIONS.items():
        for _action, (_from, to) in table.items():
            if to in terminal:
                assert to not in {t[1] for t in table.values() if _from and t[0] == (to,)}


def test_clean_field_optional_and_bounded():
    assert schema.clean_field(None, "person") is None
    assert schema.clean_field("   ", "person") is None
    assert schema.clean_field(" Gina ", "person") == "Gina"


def _projection(**over):
    p = {"object_id": "obl_" + "a" * 24, "kind": "obligation", "wording": "x",
         "person": None, "due_date": None, "project": None, "decision": None,
         "status": "open", "object_version": 1, "object_sha256": "a" * 64,
         "record_sha256": "b" * 64}
    p.update(over)
    return p


def test_verify_projection_accepts_wellformed():
    schema.verify_projection(_projection())
    schema.verify_projection(_projection(object_id="ndb_" + "c" * 24, kind="needs_bill",
                                         status="resolved", decision="no"))


def test_verify_projection_rejects_bad_status_and_id():
    with pytest.raises(schema.ValidationError):
        schema.verify_projection(_projection(status="frozen"))
    with pytest.raises(schema.ValidationError):
        schema.verify_projection(_projection(object_id="ndb_" + "a" * 24))  # prefix != kind
    with pytest.raises(schema.ValidationError):
        schema.verify_projection(_projection(object_sha256="short"))

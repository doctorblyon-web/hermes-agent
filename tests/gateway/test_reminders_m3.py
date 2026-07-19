"""M3 response verification — an unverified response is never a success.

Asserts the Williams production verifier against the accepted candidate's real
envelopes (self-sealed response_sha256, no receipt object, LISTED/APPLIED/REJECTED).
"""

import pytest

from gateway.reminders import m3_client, schema

HEX = "a" * 64
RID = "rem_" + "a" * 24


def sign(resp):
    resp = dict(resp)
    resp.pop("response_sha256", None)
    resp["response_sha256"] = schema.canonical_hash(resp)
    return resp


def projection(**over):
    p = {"reminder_id": RID, "wording": "review SignalPath",
         "due_at": "2026-07-19T23:00:00Z", "due_at_local": "2026-07-20T09:00:00+10:00",
         "timezone": "Australia/Sydney", "recurrence": None, "status": "scheduled",
         "reminder_version": 1, "reminder_sha256": HEX, "record_sha256": HEX}
    p.update(over)
    return p


def applied(operation="REMINDER_UPSERT", request_id="req1", **proj):
    return sign({"version": 1, "request_id": request_id, "operation": operation,
                 "status": "APPLIED", "applied": True, "reminder": projection(**proj),
                 "action": "create"})


UPSERT_REQ = {"operation": m3_client.OP_UPSERT, "request_id": "req1"}


def test_accepts_well_formed_applied():
    assert m3_client.verify_response(applied(), UPSERT_REQ)["status"] == "APPLIED"


def test_rejects_tampered_body():
    resp = applied()
    resp["reminder"]["reminder_version"] = 999          # mutate after signing
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_rejects_wrong_request_id():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(request_id="other"), UPSERT_REQ)


def test_rejects_applied_flag_false():
    resp = sign({"version": 1, "request_id": "req1", "operation": "REMINDER_UPSERT",
                 "status": "APPLIED", "applied": False, "reminder": projection()})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_rejects_bad_reminder_id_in_projection():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(reminder_id="R1"), UPSERT_REQ)


def test_rejects_bad_status_in_projection():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(status="weird"), UPSERT_REQ)


def test_rejects_non_hex_record_sha():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(record_sha256="nothex"), UPSERT_REQ)


def test_accepts_rejected_shape():
    resp = sign({"version": 1, "request_id": "req1", "operation": None,
                 "status": "REJECTED", "applied": False,
                 "error": {"code": "version_conflict", "message": "stale"}})
    assert m3_client.verify_response(resp, UPSERT_REQ)["status"] == "REJECTED"


def test_rejects_reject_missing_error():
    resp = sign({"version": 1, "request_id": "req1", "operation": None,
                 "status": "REJECTED", "applied": False})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_rejects_bogus_status():
    resp = sign({"version": 1, "request_id": "req1", "operation": "REMINDER_UPSERT",
                 "status": "MAYBE", "reminder": projection()})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_list_requires_listed_and_list():
    req = {"operation": m3_client.OP_LIST, "request_id": "L1"}
    good = sign({"version": 1, "request_id": "L1", "operation": "REMINDER_LIST",
                 "status": "LISTED", "count": 0, "truncated": False, "reminders": []})
    assert m3_client.verify_response(good, req)["reminders"] == []
    bad = sign({"version": 1, "request_id": "L1", "operation": "REMINDER_LIST",
                "status": "OK", "reminders": []})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(bad, req)


def test_delivery_applied_accepted():
    req = {"operation": m3_client.OP_DELIVERY, "request_id": "D1"}
    resp = sign({"version": 1, "request_id": "D1", "operation": "REMINDER_DELIVERY_RECORD",
                 "status": "APPLIED", "applied": True, "reminder": projection(status="delivered"),
                 "outcome": "delivered"})
    assert m3_client.verify_response(resp, req)["status"] == "APPLIED"


def test_builders_shape():
    up = m3_client.build_upsert(action="create", request_id="r", reminder_object={"wording": "x"},
                                approval=m3_client.approval_block("2026-07-19T09:00:00+10:00"))
    assert set(up) == {"version", "operation", "request_id", "action", "approval", "reminder"}
    assert up["approval"] == {"approved_by": "bill", "approved_at": "2026-07-19T09:00:00+10:00"}
    dr = m3_client.build_delivery_record(request_id="r", reminder_id=RID, base_version=1,
                                         attempt_id="att", outcome="delivered",
                                         detail={"attempted_at": "2026-07-20T09:00:01+10:00", "error_code": None})
    assert set(dr) == {"version", "operation", "request_id", "reminder_id", "base_version",
                       "attempt_id", "outcome", "detail"}

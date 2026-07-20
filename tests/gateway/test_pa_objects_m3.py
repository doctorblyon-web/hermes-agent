"""M3 response verification — an unverified response is never a success."""

import pytest

from gateway.pa_objects import m3_client, schema

RID = "obl_" + "a" * 24


def sign(resp):
    resp = dict(resp)
    resp.pop("response_sha256", None)
    resp["response_sha256"] = schema.canonical_hash(resp)
    return resp


def projection(**over):
    p = {"object_id": RID, "kind": "obligation", "wording": "send Gina the material",
         "person": "Gina", "due_date": "2026-07-24", "project": None, "decision": None,
         "status": "open", "object_version": 1, "object_sha256": "a" * 64,
         "record_sha256": "b" * 64}
    p.update(over)
    return p


def applied(request_id="req1", **proj):
    return sign({"version": 1, "request_id": request_id, "operation": "PA_OBJECT_UPSERT",
                 "status": "APPLIED", "applied": True, "action": "create",
                 "object": projection(**proj)})


UPSERT_REQ = {"operation": m3_client.OP_UPSERT, "request_id": "req1"}
LIST_REQ = {"operation": m3_client.OP_LIST, "request_id": "req1"}


def test_accepts_wellformed_applied():
    assert m3_client.verify_response(applied(), UPSERT_REQ)["status"] == "APPLIED"


def test_rejects_tampered_body():
    resp = applied()
    resp["object"]["status"] = "completed"        # mutate after signing
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_rejects_wrong_request_id():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(request_id="other"), UPSERT_REQ)


def test_rejects_applied_flag_false():
    resp = sign({"version": 1, "request_id": "req1", "operation": "PA_OBJECT_UPSERT",
                 "status": "APPLIED", "applied": False, "action": "create",
                 "object": projection()})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(resp, UPSERT_REQ)


def test_rejects_bad_projection_status():
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(applied(status="frozen"), UPSERT_REQ)


def test_accepts_rejected_envelope():
    resp = sign({"version": 1, "request_id": "req1", "operation": None,
                 "status": "REJECTED", "applied": False,
                 "error": {"code": "invalid_transition", "message": "no"}})
    assert m3_client.verify_response(resp, UPSERT_REQ)["status"] == "REJECTED"


def test_listed_verifies_each_projection():
    good = sign({"version": 1, "request_id": "req1", "operation": "PA_OBJECT_LIST",
                 "status": "LISTED", "count": 1, "truncated": False, "objects": [projection()]})
    assert m3_client.verify_response(good, LIST_REQ)["count"] == 1
    bad = dict(good)
    bad_objs = [projection(object_sha256="short")]
    bad = sign({"version": 1, "request_id": "req1", "operation": "PA_OBJECT_LIST",
                "status": "LISTED", "count": 1, "truncated": False, "objects": bad_objs})
    with pytest.raises(m3_client.M3Error):
        m3_client.verify_response(bad, LIST_REQ)


def test_no_delete_builder_exists():
    assert not hasattr(m3_client, "build_delete")
    assert not hasattr(m3_client, "build_purge")

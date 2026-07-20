"""Shared fakes for the PA-objects candidate tests.

``FakeM3`` is an in-memory implementation of the accepted M3 PA-object contract
(PA_OBJECT_UPSERT / PA_OBJECT_LIST). It doubles as an executable spec: it enforces
approved_by == bill, per-(kind,record_id) versioning, base_version conflicts, the
exact status-transition tables, request_id idempotency (byte-identical replay),
and it signs responses exactly as ``m3_client.verify_response`` expects.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

from gateway.pa_objects import m3_client, schema


def event(text, *, update=100, user_id="1", chat_id="9", message_id=None,
          chat_type="dm", platform="telegram"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform), chat_type=chat_type,
        user_id=user_id, chat_id=chat_id, thread_id=None,
    )
    return SimpleNamespace(
        text=text, source=source, platform_update_id=update,
        message_id=message_id or str(update),
    )


def cfg(tmp_path, **over):
    base = {"enabled": True, "bill_user_id": "1", "chat_id": "9",
            "database": str(tmp_path / "pa_objects.sqlite")}
    base.update(over)
    return base


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FakeM3:
    """In-memory canonical PA-object store faithful to the M3 endpoint."""

    M3Error = m3_client.M3Error
    build_list = staticmethod(m3_client.build_list)

    def __init__(self):
        self.records = {}     # object_id -> list of body dicts (versions)
        self.ops = {}         # request_id -> sealed response dict
        self.calls = []       # every request seen, for assertions

    # -- helpers --------------------------------------------------------------
    def _sign(self, resp):
        resp = dict(resp)
        resp.pop("response_sha256", None)
        resp["response_sha256"] = schema.canonical_hash(resp)
        return resp

    def _projection(self, body):
        return {
            "object_id": body["object_id"], "kind": body["kind"], "wording": body["wording"],
            "person": body.get("person"), "due_date": body.get("due_date"),
            "project": body.get("project"), "decision": body.get("decision"),
            "status": body["status"], "object_version": body["version"],
            "object_sha256": _sha(_canon({k: v for k, v in body.items() if k != "object_sha256"})),
            "record_sha256": _sha(_canon(body)),
        }

    def _reject(self, rid, code):
        return self._sign({"version": 1, "request_id": rid, "operation": None,
                           "status": "REJECTED", "applied": False,
                           "error": {"code": code, "message": code}})

    # -- the round trip -------------------------------------------------------
    async def call(self, request, *, timeout=12):
        self.calls.append(request)
        op = request.get("operation")
        if op == "PA_OBJECT_LIST":
            return self._list(request)
        if op != "PA_OBJECT_UPSERT":
            return m3_client.verify_response(self._reject(request.get("request_id"), "operation"), request)
        rid = request["request_id"]
        if rid in self.ops:
            return m3_client.verify_response(self.ops[rid], request)   # byte-identical replay
        resp = self._upsert(request)
        if resp["status"] == "APPLIED":
            self.ops[rid] = resp
        return m3_client.verify_response(resp, request)

    def _list(self, request):
        kind = request.get("kind")
        status = request.get("status")
        object_id = request.get("object_id")
        objects = []
        for oid, versions in self.records.items():
            head = versions[-1]
            if kind is not None and head["kind"] != kind:
                continue
            if status is not None and head["status"] != status:
                continue
            if object_id is not None and head["object_id"] != object_id:
                continue
            objects.append(self._projection(head))
        objects.sort(key=lambda o: (o["kind"], o["object_id"]))
        return m3_client.verify_response(self._sign({
            "version": 1, "request_id": request.get("request_id"), "operation": "PA_OBJECT_LIST",
            "status": "LISTED", "count": len(objects), "truncated": False, "objects": objects,
        }), request)

    def _upsert(self, request):
        rid = request["request_id"]
        kind = request.get("kind")
        spec = schema.TRANSITIONS.get(kind)
        approval = request.get("approval")
        if not isinstance(approval, dict) or approval.get("approved_by") != "bill":
            return self._reject(rid, "approval_required")
        if kind not in schema.KINDS:
            return self._reject(rid, "kind")
        action = request.get("action")
        obj = request.get("object")

        if action == "create":
            object_id = schema.ID_PREFIX[kind] + uuid.uuid4().hex[:24]
            body = {"schema_version": "pa-object-v1.2", "kind": kind, "object_id": object_id,
                    "owner": "bill", "source": "telegram", "wording": obj["wording"],
                    "content_sha256": obj["content_sha256"]}
            if kind == "obligation":
                body["person"] = obj.get("person")
                body["due_date"] = obj.get("due_date")
                body["project"] = obj.get("project")
            else:
                body["decision"] = None
            body.update({"status": "open", "version": 1, "supersedes_version": None,
                         "action": "create", "provenance": obj.get("provenance")})
            self.records[object_id] = [body]
            return self._applied(rid, body, "create")

        if action not in spec:
            return self._reject(rid, "action")
        object_id = obj.get("object_id")
        versions = self.records.get(object_id)
        if not versions:
            return self._reject(rid, "not_found")
        if not object_id.startswith(schema.ID_PREFIX[kind]):
            return self._reject(rid, "object_id")
        current = versions[-1]
        if obj.get("base_version") != current["version"]:
            return self._reject(rid, "version_conflict")
        allowed_from, to = spec[action]
        if current["status"] not in allowed_from:
            return self._reject(rid, "invalid_transition")
        body = dict(current)
        body["version"] = current["version"] + 1
        body["supersedes_version"] = current["version"]
        body["status"] = to
        body["action"] = action
        if action == "resolve":
            body["decision"] = obj.get("decision")
        versions.append(body)
        return self._applied(rid, body, action)

    def _applied(self, rid, body, action):
        return self._sign({
            "version": 1, "request_id": rid, "operation": "PA_OBJECT_UPSERT",
            "status": "APPLIED", "applied": True, "action": action,
            "object": self._projection(body),
        })

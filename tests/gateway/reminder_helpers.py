"""Shared fakes for the reliable-reminders candidate tests.

These let every test drive the full pipeline without a real scheduler, a real
Telegram send, or a real canonical write. ``FakeM3`` in particular is an
in-memory implementation of the accepted M3 reminder contract (REMINDER_UPSERT /
REMINDER_LIST / REMINDER_DELIVERY_RECORD) and doubles as an executable spec: it
enforces ``approved_by == bill``, versioning, base_version conflicts and
idempotency-on-request_id, and it signs responses exactly as
``m3_client.verify_response`` expects.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from gateway.reminders import m3_client, schema


def event(text, *, update=100, user_id="1", chat_id="9", message_id=None,
          reply_to=None, chat_type="dm", platform="telegram"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform), chat_type=chat_type,
        user_id=user_id, chat_id=chat_id, thread_id=None,
    )
    return SimpleNamespace(
        text=text, source=source, platform_update_id=update,
        message_id=message_id or str(update), reply_to_message_id=reply_to,
    )


def cfg(tmp_path, **over):
    base = {"enabled": True, "bill_user_id": "1", "chat_id": "9",
            "database": str(tmp_path / "reminders.sqlite")}
    base.update(over)
    return base


class FakeCron:
    """In-memory stand-in for the cron.jobs public surface."""

    def __init__(self):
        self.jobs = {}
        self._n = 0

    def create_job(self, *, prompt=None, schedule, name=None, repeat=None,
                   deliver=None, origin=None, script=None, no_agent=False, **kw):
        self._n += 1
        jid = f"job{self._n}"
        self.jobs[jid] = {
            "id": jid, "name": name, "schedule": {"kind": "once", "run_at": schedule},
            "next_run_at": schedule, "deliver": deliver, "origin": origin,
            "script": script, "no_agent": no_agent,
            "repeat": {"times": repeat, "completed": 0}, "enabled": True,
        }
        return self.jobs[jid]

    def get_job(self, jid):
        return self.jobs.get(jid)

    def remove_job(self, jid):
        return self.jobs.pop(jid, None) is not None

    def list_jobs(self, include_disabled=False):
        return list(self.jobs.values())

    def update_job(self, jid, updates):
        job = self.jobs.get(jid)
        if job:
            job.update(updates)
        return job


class FakeDeliver:
    """Reused Telegram-send stand-in: records every send, returns (ok, error)."""

    def __init__(self, ok=True, error=None, raises=None):
        self.ok, self.error, self.raises = ok, error, raises
        self.sent = []

    def __call__(self, target, text):
        self.sent.append((target, text))
        if self.raises is not None:
            raise self.raises
        return (self.ok, self.error)


class FakeBridge:
    """In-memory stand-in for scheduler_bridge (create/replace/remove/verify).

    ``fail_next`` makes the next create/replace raise, to exercise the
    "M3 succeeded but local scheduling failed" honesty path.
    """

    def __init__(self):
        self.jobs = {}            # reminder_id -> job_id
        self._n = 0
        self.fail_next = False

    def _maybe_fail(self):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("local scheduling failed")

    def create_oneshot(self, *, reminder_id, version, due_rfc3339, target, cron=None):
        self._maybe_fail()
        self._n += 1
        job_id = f"job{self._n}"
        self.jobs[reminder_id] = job_id
        return job_id

    def replace_oneshot(self, *, reminder_id, version, due_rfc3339, target,
                        old_cron_job_id, cron=None):
        self._maybe_fail()
        self._n += 1
        job_id = f"job{self._n}"
        self.jobs[reminder_id] = job_id
        return job_id

    def remove_oneshot(self, cron_job_id, reminder_id=None, cron=None):
        if reminder_id in self.jobs:
            del self.jobs[reminder_id]
        return True

    def verify_linked(self, job_id, reminder_id, cron=None):
        return self.jobs.get(reminder_id) == job_id

    @staticmethod
    def target_json(target):
        import json
        return json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FakeM3:
    """In-memory canonical M3 faithful to the accepted candidate contract.

    Mirrors ``process-event.candidate-reminders-20260719T064236Z``: approval must
    be {approved_by:"bill", approved_at}, REMINDER_UPSERT reminder objects are the
    exact per-action schemas, LIST returns status "LISTED" with the full
    projection, delivery records are idempotent on (attempt_id, outcome), and
    every response is self-sealed with response_sha256 exactly as the endpoint.
    """

    def __init__(self, *, fail=False):
        self.reminders = {}      # reminder_id -> canonical body dict
        self.delivery = {}       # (reminder_id, attempt_id, outcome) -> response (idempotency)
        self.recorded = []       # ordered outcomes actually recorded (new versions)
        self._seen = {}          # request_id -> cached write response (idempotency)
        self.fail = fail
        self.calls = []

    def build_upsert(self, **kw):
        return m3_client.build_upsert(**kw)

    def build_list(self, **kw):
        return m3_client.build_list(**kw)

    def build_delivery_record(self, **kw):
        return m3_client.build_delivery_record(**kw)

    def _sign(self, resp):
        resp = dict(resp)
        resp.pop("response_sha256", None)
        resp["response_sha256"] = schema.canonical_hash(resp)
        return resp

    def _reject(self, request, code, msg):
        return self._sign({
            "version": 1, "request_id": request.get("request_id"), "operation": None,
            "status": "REJECTED", "applied": False, "error": {"code": code, "message": msg},
        })

    @staticmethod
    def _due_local(due_at, tz):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).isoformat()

    def _projection(self, body):
        return {
            "reminder_id": body["reminder_id"], "wording": body["wording"],
            "due_at": body["due_at"], "due_at_local": body["due_at_local"],
            "timezone": body["timezone"], "recurrence": body["recurrence"],
            "status": body["status"], "reminder_version": body["version"],
            "reminder_sha256": schema.canonical_hash(body),
            "record_sha256": schema.canonical_hash({"r": body, "v": body["version"]}),
        }

    def _applied(self, request, body, extra):
        out = {
            "version": 1, "request_id": request["request_id"],
            "operation": request["operation"], "status": "APPLIED", "applied": True,
            "reminder": self._projection(body),
        }
        out.update(extra)
        return self._sign(out)

    async def call(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("transport down")
        op = request["operation"]
        if op == m3_client.OP_LIST:
            status = request.get("status", "scheduled")
            rems = [self._projection(b) for b in self.reminders.values()
                    if status is None or b["status"] == status]
            return self._sign({
                "version": 1, "request_id": request.get("request_id"),
                "operation": "REMINDER_LIST", "status": "LISTED",
                "count": len(rems), "truncated": False, "reminders": rems,
            })
        rid = request["request_id"]
        if rid in self._seen:
            return self._seen[rid]
        resp = self._upsert(request) if op == m3_client.OP_UPSERT else self._delivery(request)
        self._seen[rid] = resp
        return resp

    def _upsert(self, request):
        approval = request.get("approval") or {}
        if approval.get("approved_by") != "bill" or not approval.get("approved_at"):
            return self._reject(request, "approval_required", "visible Bill approval is required")
        action = request["action"]
        r = request["reminder"]
        if action == "create":
            rid = "rem_" + uuid.uuid4().hex[:24]
            tz = r["timezone"]
            body = {"reminder_id": rid, "wording": r["wording"], "due_at": r["due_at"],
                    "due_at_local": self._due_local(r["due_at"], tz), "timezone": tz,
                    "recurrence": r["recurrence"], "status": "scheduled", "version": 1}
            self.reminders[rid] = body
            return self._applied(request, body, {"action": "create"})
        rid = r["reminder_id"]
        cur = self.reminders.get(rid)
        if cur is None:
            return self._reject(request, "not_found", "reminder does not exist")
        if r.get("base_version") != cur["version"]:
            return self._reject(request, "version_conflict", "base_version mismatch")
        body = dict(cur)
        body["version"] = cur["version"] + 1
        if action == "cancel":
            if cur["status"] not in ("scheduled", "uncertain"):
                return self._reject(request, "not_cancellable", "not live")
            body["status"] = "cancelled"
        elif action == "snooze":
            if cur["status"] != "scheduled":
                return self._reject(request, "not_mutable", "not scheduled")
            body["due_at"] = r["due_at"]
            body["due_at_local"] = self._due_local(r["due_at"], body["timezone"])
            body["status"] = "scheduled"
        else:  # change
            if cur["status"] != "scheduled":
                return self._reject(request, "not_mutable", "not scheduled")
            body.update(wording=r["wording"], due_at=r["due_at"], timezone=r["timezone"],
                        recurrence=r["recurrence"], status="scheduled")
            body["due_at_local"] = self._due_local(r["due_at"], r["timezone"])
        self.reminders[rid] = body
        return self._applied(request, body, {"action": action})

    def _delivery(self, request):
        rid = request["reminder_id"]
        cur = self.reminders.get(rid)
        if cur is None:
            return self._reject(request, "not_found", "reminder does not exist")
        key = (rid, request["attempt_id"], request["outcome"])
        if key in self.delivery:
            return self.delivery[key]
        if request.get("base_version") != cur["version"]:
            return self._reject(request, "version_conflict", "base_version mismatch")
        if cur["status"] not in ("scheduled", "uncertain"):
            return self._reject(request, "not_deliverable", "cannot record from %s" % cur["status"])
        body = dict(cur)
        body["version"] = cur["version"] + 1
        body["status"] = request["outcome"]
        self.reminders[rid] = body
        resp = self._applied(request, body, {"outcome": request["outcome"]})
        self.delivery[key] = resp
        self.recorded.append(request["outcome"])
        return resp

    # test convenience
    def seed(self, reminder_id, *, version=1, status="scheduled", wording="do a thing",
             due_at="2026-07-19T23:00:00Z", timezone="Australia/Sydney", recurrence=None):
        self.reminders[reminder_id] = {
            "reminder_id": reminder_id, "wording": wording, "due_at": due_at,
            "due_at_local": self._due_local(due_at, timezone), "timezone": timezone,
            "recurrence": recurrence, "status": status, "version": version,
        }

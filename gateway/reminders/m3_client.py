"""M3 canonical reminder client — reconciled to the accepted M3 candidate.

Wire contract source of truth: ``process-event.candidate-reminders-20260719T064236Z``
(SHA ``3e88e9b4…269a``), reminder endpoint (``--reminder-endpoint``). Verified by
a real cross-machine probe (see the combined evidence packet).

Transport mirrors the proven signal-gate SSH round-trip. The response is trusted
only after its self-sealed ``response_sha256`` is reproduced and its correlation
fields check out — an unverifiable response is an error, never a success.

Response envelopes (no separate receipt object — the seal IS the receipt):
  APPLIED  : {version, request_id, operation, status:"APPLIED", applied:true,
              reminder:{reminder_id, wording, due_at, due_at_local, timezone,
                        recurrence, status, reminder_version, reminder_sha256,
                        record_sha256}, action|outcome, response_sha256}
  LISTED   : {version, request_id, operation:"REMINDER_LIST", status:"LISTED",
              count, truncated, reminders:[projection…], response_sha256}
  REJECTED : {version, request_id, operation:null, status:"REJECTED",
              applied:false, error:{code, message}, response_sha256}
"""

from __future__ import annotations

import asyncio
import json
import re

from gateway.reminders.schema import REMINDER_ID_RE, STATUSES, canonical_hash

SSH = "/usr/bin/ssh"
IDENTITY = "/Users/willlyon/.ssh/billos_signal_v0"
M3_USER = "billlyon"
M3_HOST = "100.122.219.24"

OP_UPSERT = "REMINDER_UPSERT"
OP_LIST = "REMINDER_LIST"
OP_DELIVERY = "REMINDER_DELIVERY_RECORD"

_HEX64 = re.compile(r"[0-9a-f]{64}")


class M3Error(RuntimeError):
    pass


def _verify_projection(reminder) -> None:
    if not isinstance(reminder, dict):
        raise M3Error("reminder_shape")
    rid = reminder.get("reminder_id")
    if not isinstance(rid, str) or not REMINDER_ID_RE.fullmatch(rid or ""):
        raise M3Error("reminder_id")
    if not isinstance(reminder.get("reminder_version"), int) or isinstance(reminder.get("reminder_version"), bool):
        raise M3Error("reminder_version")
    if reminder.get("status") not in STATUSES:
        raise M3Error("reminder_status")
    for key in ("reminder_sha256", "record_sha256"):
        if not _HEX64.fullmatch(str(reminder.get(key) or "")):
            raise M3Error(f"{key}_format")
    if not isinstance(reminder.get("wording"), str) or not isinstance(reminder.get("due_at"), str):
        raise M3Error("projection_fields")


def verify_response(response, request):
    """Validate a raw M3 reminder response against its request. Strict: any
    integrity or correlation failure raises, so success is never claimed on an
    unverified reply."""
    if not isinstance(response, dict) or len(json.dumps(response).encode()) > 32768:
        raise M3Error("invalid_response")
    supplied = response.get("response_sha256")
    unsigned = dict(response)
    unsigned.pop("response_sha256", None)
    if supplied != canonical_hash(unsigned):
        raise M3Error("response_hash")

    req_id = request.get("request_id")
    if req_id is not None and response.get("request_id") != req_id:
        raise M3Error("correlation_request_id")

    status = response.get("status")

    if status == "REJECTED":
        if response.get("applied") is not False:
            raise M3Error("reject_applied_flag")
        error = response.get("error")
        if not isinstance(error, dict) or not error.get("code"):
            raise M3Error("reject_error")
        return response

    operation = request.get("operation")

    if operation == OP_LIST:
        if status != "LISTED" or not isinstance(response.get("reminders"), list):
            raise M3Error("list_shape")
        return response

    # REMINDER_UPSERT / REMINDER_DELIVERY_RECORD APPLIED
    if status != "APPLIED" or response.get("applied") is not True:
        raise M3Error("status")
    if response.get("operation") != operation:
        raise M3Error("operation_echo")
    _verify_projection(response.get("reminder"))
    return response


async def _read_bounded(stream, limit):
    chunks, size = [], 0
    while True:
        chunk = await stream.read(min(4096, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise M3Error("output_limit")
        chunks.append(chunk)


async def _kill(proc):
    if proc.returncode is None:
        proc.kill()
        await proc.wait()


async def call(request, *, timeout=12):
    """Send one verified request/response round-trip to canonical M3."""
    argv = [
        SSH, "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
        "-o", "ConnectTimeout=5", "-i", IDENTITY, f"{M3_USER}@{M3_HOST}",
    ]
    body = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > 8192:
        raise M3Error("request_too_large")
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout_task = asyncio.create_task(_read_bounded(proc.stdout, 32768))
    stderr_task = asyncio.create_task(_read_bounded(proc.stderr, 4096))
    try:
        assert proc.stdin is not None
        proc.stdin.write(body)
        await proc.stdin.drain()
        proc.stdin.close()
        _rc, out, _err = await asyncio.wait_for(
            asyncio.gather(proc.wait(), stdout_task, stderr_task), timeout
        )
    except asyncio.TimeoutError:
        await _kill(proc)
        raise M3Error("indeterminate_timeout")
    except M3Error:
        await _kill(proc)
        raise
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
    if proc.returncode != 0:
        raise M3Error("indeterminate_transport")
    try:
        response = json.loads(out)
    except ValueError as exc:
        raise M3Error("invalid_json") from exc
    return verify_response(response, request)


# --- request builders (exact M3 candidate schemas) --------------------------

def build_upsert(*, action, request_id, reminder_object, approval):
    """REMINDER_UPSERT. ``approval`` must be {approved_by:"bill", approved_at:<aware RFC3339>};
    ``reminder_object`` is the exact per-action object from schema.reminder_object()."""
    return {
        "version": 1,
        "operation": OP_UPSERT,
        "request_id": request_id,
        "action": action,
        "approval": approval,
        "reminder": reminder_object,
    }


def build_list(*, request_id=None, status="scheduled"):
    req = {"version": 1, "operation": OP_LIST}
    if request_id is not None:
        req["request_id"] = request_id
    if status is not None:
        req["status"] = status
    return req


def build_delivery_record(*, request_id, reminder_id, base_version, attempt_id, outcome, detail):
    """REMINDER_DELIVERY_RECORD. ``detail`` must be exactly
    {attempted_at:<aware RFC3339>, error_code:<str|null>}."""
    return {
        "version": 1,
        "operation": OP_DELIVERY,
        "request_id": request_id,
        "reminder_id": reminder_id,
        "base_version": base_version,
        "attempt_id": attempt_id,
        "outcome": outcome,
        "detail": detail,
    }


def approval_block(approved_at):
    return {"approved_by": "bill", "approved_at": approved_at}

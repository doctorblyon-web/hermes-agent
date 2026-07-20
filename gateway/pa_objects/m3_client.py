"""M3 canonical PA-object client.

Transport mirrors the proven signal-gate / reminders SSH round-trip exactly: the
request is written on stdin to the command-restricted signal endpoint, which peeks
the operation and hands ``PA_OBJECT_*`` to ``process-event --pa-object-endpoint``.
The response is trusted only after its self-sealed ``response_sha256`` is
reproduced and its ``request_id`` correlates — an unverifiable response is an
error, never a success.

Envelopes:
  APPLIED  : {version, request_id, operation:"PA_OBJECT_UPSERT", status:"APPLIED",
              applied:true, action, object:{projection…}, response_sha256}
  LISTED   : {version, request_id, operation:"PA_OBJECT_LIST", status:"LISTED",
              count, truncated, objects:[projection…], response_sha256}
  REJECTED : {version, request_id, operation:null, status:"REJECTED",
              applied:false, error:{code, message}, response_sha256}
"""

from __future__ import annotations

import asyncio
import json

from gateway.pa_objects.schema import canonical_hash, verify_projection, ValidationError

SSH = "/usr/bin/ssh"
IDENTITY = "/Users/willlyon/.ssh/billos_signal_v0"
M3_USER = "billlyon"
M3_HOST = "100.122.219.24"

OP_UPSERT = "PA_OBJECT_UPSERT"
OP_LIST = "PA_OBJECT_LIST"


class M3Error(RuntimeError):
    pass


def verify_response(response, request):
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
        if status != "LISTED" or not isinstance(response.get("objects"), list):
            raise M3Error("list_shape")
        for obj in response["objects"]:
            try:
                verify_projection(obj)
            except ValidationError as exc:
                raise M3Error(f"list_projection:{exc.code}")
        return response

    # PA_OBJECT_UPSERT APPLIED
    if status != "APPLIED" or response.get("applied") is not True:
        raise M3Error("status")
    if response.get("operation") != operation:
        raise M3Error("operation_echo")
    try:
        verify_projection(response.get("object"))
    except ValidationError as exc:
        raise M3Error(f"projection:{exc.code}")
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
        SSH, "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=5",
        "-i", IDENTITY, f"{M3_USER}@{M3_HOST}",
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

def approval_block(approved_at):
    return {"approved_by": "bill", "approved_at": approved_at}


def build_create(*, request_id, kind, obj, approval):
    return {
        "version": 1,
        "operation": OP_UPSERT,
        "request_id": request_id,
        "kind": kind,
        "action": "create",
        "approval": approval,
        "object": obj,
    }


def build_transition(*, request_id, kind, action, obj, approval):
    return {
        "version": 1,
        "operation": OP_UPSERT,
        "request_id": request_id,
        "kind": kind,
        "action": action,
        "approval": approval,
        "object": obj,
    }


def build_list(*, request_id=None, kind=None, status=None, object_id=None):
    req = {"version": 1, "operation": OP_LIST}
    if request_id is not None:
        req["request_id"] = request_id
    if kind is not None:
        req["kind"] = kind
    if status is not None:
        req["status"] = status
    if object_id is not None:
        req["object_id"] = object_id
    return req

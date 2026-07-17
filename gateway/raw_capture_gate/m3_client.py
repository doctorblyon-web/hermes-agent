import asyncio
import hashlib
import json
import re
from datetime import datetime

from gateway.raw_capture_gate.schema import REQUEST_ENVELOPE_BYTES, content_sha256


class M3Error(RuntimeError):
    pass


SSH = "/usr/bin/ssh"
IDENTITY = "/Users/willlyon/.ssh/billos_raw_capture_v0"
M3_USER = "billlyon"
M3_HOST = "100.122.219.24"
EVENT_ID_RE = re.compile(r"RAW_CAPTURE_APPEND-[0-9]{8}T[0-9]{12}Z-bill")
CAPTURE_ID_RE = re.compile(r"cap_[0-9a-f]{32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ACTOR_RE = re.compile(r"[A-Za-z0-9._@-]{1,128}")
APPLIED_KEYS = {
    "version", "request_id", "status", "capture_id", "content_sha256",
    "event_id", "operation", "event_created_at", "applied_at",
    "process_receipt", "response_sha256",
}
RECEIPT_KEYS = {
    "receipt_id", "receipt_event_id", "receipt_sha256", "operation",
    "request_id", "capture_id", "content_sha256", "canonical_state_sha256",
    "applied_by", "processed_at", "updated_by", "receipt_recovered",
}
FAILURE_KEYS = {"version", "request_id", "status", "applied", "error", "response_sha256"}


def canonical_hash(obj):
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def _aware_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def verify_response(response, request):
    if not isinstance(response, dict) or len(json.dumps(response).encode()) > 32768:
        raise M3Error("invalid_response")
    supplied = response.get("response_sha256")
    unsigned = dict(response)
    unsigned.pop("response_sha256", None)
    if supplied != canonical_hash(unsigned):
        raise M3Error("response_hash")
    if response.get("version") != 1 or response.get("request_id") != request.get("request_id"):
        raise M3Error("request_correlation")
    if response.get("status") == "APPLIED":
        if set(response) != APPLIED_KEYS:
            raise M3Error("applied_schema")
        event_id, capture_id = response.get("event_id"), response.get("capture_id")
        if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
            raise M3Error("event_id")
        if not isinstance(capture_id, str) or CAPTURE_ID_RE.fullmatch(capture_id) is None:
            raise M3Error("capture_id")
        if response.get("operation") != "RAW_CAPTURE_APPEND":
            raise M3Error("operation")
        if not _aware_timestamp(response.get("event_created_at")) or not _aware_timestamp(response.get("applied_at")):
            raise M3Error("response_timestamp")
        expected_content = content_sha256(request["capture"]["original_text"])
        if response.get("content_sha256") != expected_content:
            raise M3Error("content_correlation")
        receipt = response.get("process_receipt")
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
            raise M3Error("receipt_schema")
        if receipt["receipt_event_id"] != event_id:
            raise M3Error("receipt_event_correlation")
        if not isinstance(receipt["receipt_id"], str) or re.fullmatch(
            re.escape(event_id) + r"\.(?:[0-9]{8}T[0-9]{6}Z|recovered)\.txt",
            receipt["receipt_id"],
        ) is None:
            raise M3Error("receipt_identity_correlation")
        if any((
            receipt.get("operation") != "RAW_CAPTURE_APPEND",
            receipt.get("request_id") != request.get("request_id"),
            receipt.get("capture_id") != capture_id,
            receipt.get("content_sha256") != expected_content,
            receipt.get("applied_by") != "process-event-v0.3",
            ACTOR_RE.fullmatch(receipt.get("updated_by", "")) is None,
            not _aware_timestamp(receipt.get("processed_at")),
            type(receipt.get("receipt_recovered")) is not bool,
        )):
            raise M3Error("receipt_correlation")
        if receipt["receipt_recovered"] != receipt["receipt_id"].endswith(".recovered.txt"):
            raise M3Error("receipt_recovery_correlation")
        for key in ("receipt_sha256", "canonical_state_sha256", "content_sha256"):
            if not isinstance(receipt[key], str) or SHA256_RE.fullmatch(receipt[key]) is None:
                raise M3Error(f"receipt_{key}_format")
    elif response.get("status") in {"INDETERMINATE", "REJECTED"}:
        if set(response) != FAILURE_KEYS or not isinstance(response.get("error"), dict) or set(response["error"]) != {"code", "message"}:
            raise M3Error("failure_schema")
        if not all(isinstance(response["error"].get(key), str) and response["error"][key] for key in ("code", "message")):
            raise M3Error("failure_error")
        if response["status"] == "INDETERMINATE" and response.get("applied") is not None:
            raise M3Error("indeterminate_status")
        if response["status"] == "REJECTED" and response.get("applied") is not False:
            raise M3Error("rejected_status")
    else:
        raise M3Error("status")
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


def ssh_argv():
    return [
        SSH, "-F", "/dev/null", "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
        "-o", "PermitLocalCommand=no", "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=5", "-T", "-i", IDENTITY,
        f"{M3_USER}@{M3_HOST}",
    ]


async def call_serialized(request_json, *, timeout=12):
    argv = ssh_argv()
    if not isinstance(request_json, str):
        raise M3Error("request_type")
    body = request_json.encode("utf-8")
    if len(body) > REQUEST_ENVELOPE_BYTES:
        raise M3Error("request_too_large")
    try:
        request = json.loads(request_json)
    except ValueError as exc:
        raise M3Error("invalid_request_json") from exc
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_task = asyncio.create_task(_read_bounded(proc.stdout, 32768))
    stderr_task = asyncio.create_task(_read_bounded(proc.stderr, 4096))
    try:
        proc.stdin.write(body)
        await proc.stdin.drain()
        proc.stdin.close()
        _, out, _ = await asyncio.wait_for(
            asyncio.gather(proc.wait(), stdout_task, stderr_task), timeout,
        )
    except asyncio.TimeoutError as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise M3Error("indeterminate_timeout") from exc
    except M3Error:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
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


async def call(request, *, timeout=12):
    request_json = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return await call_serialized(request_json, timeout=timeout)

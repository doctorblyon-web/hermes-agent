import asyncio
import hashlib
import json
import re


class M3Error(RuntimeError):
    pass


SSH = "/usr/bin/ssh"
IDENTITY = "/Users/willlyon/.ssh/billos_signal_v0"
M3_USER = "billlyon"
M3_HOST = "100.122.219.24"


def canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def verify_response(response, request):
    if not isinstance(response, dict) or len(json.dumps(response).encode()) > 32768:
        raise M3Error("invalid_response")
    supplied = response.get("response_sha256")
    unsigned = dict(response); unsigned.pop("response_sha256", None)
    if supplied != canonical_hash(unsigned):
        raise M3Error("response_hash")
    for key in ("request_id", "proposal_id", "proposal_sha256"):
        if response.get(key) != request.get(key):
            raise M3Error(f"correlation_{key}")
    if response.get("status") == "APPLIED":
        receipt = response.get("process_receipt") or {}
        if response.get("event_type") != "SIGNAL_SET" or not response.get("event_id"):
            raise M3Error("event_correlation")
        for key in ("receipt_id", "receipt_event_id", "receipt_sha256", "canonical_state_sha256"):
            if not receipt.get(key):
                raise M3Error(f"receipt_{key}")
        if receipt["receipt_event_id"] != response["event_id"]:
            raise M3Error("receipt_event_correlation")
        if not receipt["receipt_id"].startswith(response["event_id"] + "."):
            raise M3Error("receipt_identity_correlation")
        for key in ("receipt_sha256", "canonical_state_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
                raise M3Error(f"receipt_{key}_format")
    elif response.get("status") != "REJECTED" or response.get("applied") is not False:
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


async def _kill(proc):
    if proc.returncode is None:
        proc.kill()
        await proc.wait()


async def call(request, *, timeout=12):
    argv = [SSH, "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=5", "-i", IDENTITY, f"{M3_USER}@{M3_HOST}"]
    body = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > 8192:
        raise M3Error("request_too_large")
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_task = asyncio.create_task(_read_bounded(proc.stdout, 32768))
    stderr_task = asyncio.create_task(_read_bounded(proc.stderr, 4096))
    try:
        assert proc.stdin is not None
        proc.stdin.write(body)
        await proc.stdin.drain()
        proc.stdin.close()
        _returncode, out, _err = await asyncio.wait_for(
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

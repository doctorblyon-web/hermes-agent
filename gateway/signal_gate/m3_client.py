import asyncio
import hashlib
import json


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
        for key in ("receipt_id", "receipt_sha256", "canonical_state_sha256"):
            if not receipt.get(key):
                raise M3Error(f"receipt_{key}")
    elif response.get("status") != "REJECTED" or response.get("applied") is not False:
        raise M3Error("status")
    return response


async def call(request, *, timeout=12):
    argv = [SSH, "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=5", "-i", IDENTITY, f"{M3_USER}@{M3_HOST}"]
    body = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > 8192:
        raise M3Error("request_too_large")
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(body), timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait(); raise M3Error("indeterminate_timeout")
    if len(out) > 32768 or proc.returncode != 0:
        raise M3Error("indeterminate_transport")
    try:
        response = json.loads(out)
    except ValueError as exc:
        raise M3Error("invalid_json") from exc
    return verify_response(response, request)

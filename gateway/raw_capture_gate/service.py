import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from gateway.raw_capture_gate import m3_client, schema
from gateway.raw_capture_gate.store import Store
from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)
INDETERMINATE_REPLY = (
    "Your capture may have reached M3, but canonical completion could not be verified. "
    "It remains indeterminate and no success or failure is being claimed."
)


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    event_id: str
    content_sha256: str
    receipt_sha256: str
    source_update_id: str
    source_message_id: str


def settings():
    gateway = load_config_readonly().get("gateway", {})
    raw = gateway.get("raw_capture_gate", {}) if isinstance(gateway, dict) else {}
    return raw if isinstance(raw, dict) else {"enabled": False}


def _store(cfg):
    path = cfg.get("database") or str(
        get_hermes_home() / "raw_capture_gate" / "transactions.sqlite"
    )
    return Store(Path(path).expanduser())


def _platform(source):
    return getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_edited(event):
    raw = getattr(event, "raw_message", None)
    return getattr(raw, "edit_date", None) is not None


async def _reconcile_transaction(store, txn, *, timeout):
    if not store.record_attempt(txn.request_id):
        return
    try:
        response = await m3_client.call_serialized(txn.request_json, timeout=timeout)
    except Exception as exc:
        logger.warning("Raw capture reconciliation remains indeterminate: %s", exc)
        store.indeterminate(txn.request_id, "reconcile_transport_or_verification")
        return
    if response["status"] == "APPLIED":
        receipt = response["process_receipt"]
        store.applied(txn.request_id, response["event_id"], receipt["receipt_sha256"])
    elif response["status"] == "REJECTED":
        store.failed(txn.request_id, (response.get("error") or {}).get("code", "m3_rejected"))
    else:
        store.indeterminate(
            txn.request_id, (response.get("error") or {}).get("code", "m3_indeterminate")
        )


async def reconcile_inflight(
    *, max_rows=4, max_attempts=4, timeout=4, total_timeout=8, cfg=None, store=None
):
    """Silently reconcile a bounded batch without recreating user messages."""
    cfg = settings() if cfg is None else cfg
    if cfg.get("enabled") is not True:
        return 0
    store = _store(cfg) if store is None else store
    rows = store.inflight(limit=max_rows, max_attempts=max_attempts)
    if not rows:
        return 0

    async def run_batch():
        for txn in rows:
            await _reconcile_transaction(store, txn, timeout=timeout)

    try:
        await asyncio.wait_for(run_batch(), timeout=total_timeout)
    except asyncio.TimeoutError:
        logger.warning("Raw capture reconciliation batch reached its total timeout")
    return len(rows)


def _request_hash(event, parsed, captured_at):
    identity = {
        "source": {
            "platform": "telegram",
            "user_id": str(event.source.user_id),
            "chat_id": str(event.source.chat_id),
            "update_id": str(event.platform_update_id),
            "message_id": str(event.message_id),
            "captured_at": captured_at,
        },
        "capture": {
            "original_text": parsed.original_text,
            "content_sha256": schema.content_sha256(parsed.original_text),
            "triage_state": parsed.triage_state,
            "triage_reason": parsed.triage_reason,
        },
    }
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def _build_request(request_id, event, parsed, captured_at):
    return {
        "version": 1,
        "operation": "RAW_CAPTURE_APPEND",
        "request_id": request_id,
        "source": {
            "platform": "telegram",
            "user_id": str(event.source.user_id),
            "chat_id": str(event.source.chat_id),
            "update_id": str(event.platform_update_id),
            "message_id": str(event.message_id),
            "captured_at": captured_at,
        },
        "capture": {
            "original_text": parsed.original_text,
            "content_sha256": schema.content_sha256(parsed.original_text),
            "triage_state": parsed.triage_state,
            "triage_reason": parsed.triage_reason,
        },
    }


async def _capture(event, parsed, *, structured=False):
    """Run an already-validated capture through the existing durable path."""
    if _is_edited(event):
        return None
    try:
        cfg = settings()
    except Exception as exc:
        logger.error("Raw capture configuration unavailable: %s", exc)
        return "Raw Capture is currently unavailable; no canonical outcome is being claimed."
    if cfg.get("enabled") is not True:
        return None
    source = event.source
    lane_ok = (
        _platform(source) == "telegram"
        and getattr(source, "chat_type", None) == "dm"
        and str(getattr(source, "user_id", None)) == str(cfg.get("bill_user_id"))
        and str(getattr(source, "chat_id", None)) == str(cfg.get("chat_id"))
    )
    if not lane_ok:
        return None
    if event.media_urls or event.media_types:
        return "Raw capture currently accepts text only. Nothing was recorded."
    if event.platform_update_id is None or event.message_id is None:
        return "Raw capture requires Telegram update and message identity. Nothing was recorded."

    try:
        captured_at = schema.source_timestamp(event.timestamp)
    except schema.CaptureError as exc:
        return f"Capture rejected: {exc}. Nothing was recorded."

    try:
        store = _store(cfg)
        await reconcile_inflight(
            max_rows=1, max_attempts=4, timeout=3, total_timeout=3.5,
            cfg=cfg, store=store,
        )
    except Exception as exc:
        logger.error("Raw capture durable state unavailable: %s", exc)
        return "Raw Capture is currently unavailable; no canonical outcome is being claimed."
    request_sha = _request_hash(event, parsed, captured_at)
    candidate_request_id = str(uuid.uuid4())
    candidate_request = _build_request(candidate_request_id, event, parsed, captured_at)
    candidate_request_json = _canonical(candidate_request)
    txn, identical = store.begin(
        request_id=candidate_request_id,
        update_id=event.platform_update_id,
        message_id=event.message_id,
        request_sha256=request_sha,
        request_json=candidate_request_json,
    )
    if not identical:
        return "That Telegram update was already bound to different capture content. Nothing was recorded."

    try:
        response = await m3_client.call_serialized(txn.request_json, timeout=12)
    except Exception as exc:
        logger.error("Raw capture outcome indeterminate: %s", exc)
        store.indeterminate(txn.request_id, "transport_or_verification")
        return INDETERMINATE_REPLY

    if response["status"] == "APPLIED":
        receipt = response["process_receipt"]
        current = store.get(txn.request_id)
        if current.state != "APPLIED" and not store.applied(
            txn.request_id, response["event_id"], receipt["receipt_sha256"]
        ):
            return (
                "M3 returned a verified receipt, but Williams could not durably record it. "
                "The capture remains indeterminate."
            )
        if structured:
            return CaptureResult(
                capture_id=response["capture_id"],
                event_id=response["event_id"],
                content_sha256=response["content_sha256"],
                receipt_sha256=receipt["receipt_sha256"],
                source_update_id=str(event.platform_update_id),
                source_message_id=str(event.message_id),
            )
        return (
            f"Captured canonically on M3 as {response['capture_id']}. "
            f"Receipt {receipt['receipt_id']} was verified."
        )

    if response["status"] == "INDETERMINATE":
        code = (response.get("error") or {}).get("code", "m3_indeterminate")
        store.indeterminate(txn.request_id, code)
        return INDETERMINATE_REPLY

    code = (response.get("error") or {}).get("code", "m3_rejected")
    store.failed(txn.request_id, code)
    return f"M3 rejected the capture ({code}). Nothing was recorded."


async def capture_text(event, text):
    """Capture caller-supplied source text through the existing durable path."""
    try:
        parsed = schema.validate_text(text)
    except schema.CaptureError as exc:
        return f"Capture rejected: {exc}. Nothing was recorded."
    return await _capture(event, parsed)


async def capture_result(event, text):
    """Return verified capture provenance for a deterministic internal route."""
    try:
        parsed = schema.validate_text(text)
    except schema.CaptureError:
        return None
    result = await _capture(event, parsed, structured=True)
    return result if isinstance(result, CaptureResult) else None


async def intercept(event):
    """Return None for baseline routing or a truthful capture response."""
    if _is_edited(event) or not schema.is_capture_command(getattr(event, "text", None)):
        return None
    try:
        parsed = schema.parse_capture(event.text)
    except schema.CaptureError as exc:
        return f"Capture rejected: {exc}. Nothing was recorded."
    if parsed is None:
        return None
    return await _capture(event, parsed)

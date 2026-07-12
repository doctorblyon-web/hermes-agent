import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home
from gateway.signal_gate import m3_client, schema
from gateway.signal_gate.store import Store

logger = logging.getLogger(__name__)


class GovernedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prepared:
    proposal_id: str
    store: Store


def settings():
    gateway = load_config_readonly().get("gateway", {})
    raw = gateway.get("signal_gate", {}) if isinstance(gateway, dict) else {}
    if not isinstance(raw, dict):
        return {"enabled": False}
    return raw


def _store(cfg):
    path = cfg.get("database") or str(get_hermes_home() / "signal_gate.db")
    return Store(Path(path).expanduser())


def prepare(adapter, event, text, *, has_attachments=False):
    cfg = settings()
    if not cfg.get("enabled"):
        return None
    proposal = schema.parse_display(text)
    if proposal is None:
        if "```signal" in (text or "") or "approve canonical application" in (text or "").lower():
            raise GovernedError("invalid governed SIGNAL envelope")
        return None
    if adapter.name != "telegram" or has_attachments:
        raise GovernedError("governed SIGNAL requires one text-only Telegram message")
    if str(event.source.user_id) != str(cfg.get("bill_user_id")) or str(event.source.chat_id) != str(cfg.get("chat_id")):
        raise GovernedError("governed SIGNAL is restricted to the configured private lane")
    canonical = schema.canonical_json(proposal)
    pid = _store(cfg).prepare(
        bill_user_id=event.source.user_id, chat_id=event.source.chat_id,
        source_update_id=event.platform_update_id, source_message_id=event.message_id,
        display_sha256=schema.sha256(text), proposal_sha256=schema.sha256(canonical),
        proposal_json=canonical, ttl=1800,
    )
    return Prepared(pid, _store(cfg))


def finalize(prepared, result):
    ids = [str(x) for x in ([result.message_id] + list(getattr(result, "continuation_message_ids", ()) or ())) if x]
    raw = getattr(result, "raw_response", None) or {}
    ids += [str(x) for x in raw.get("message_ids", ()) if x and str(x) not in ids]
    if result.success and len(ids) == 1 and prepared.store.arm(prepared.proposal_id, ids[0]):
        return True
    prepared.store.delivery_failed(prepared.proposal_id)
    return False


def _request(txn, event, request_id):
    return {
        "version": 1, "operation": "SIGNAL_SET", "request_id": request_id,
        "proposal_id": txn.id, "proposal_sha256": txn.proposal_sha256,
        "approved_by": {"platform": "telegram", "user_id": str(event.source.user_id),
            "chat_id": str(event.source.chat_id), "proposal_message_id": txn.proposal_message_id,
            "approval_update_id": str(event.platform_update_id), "approval_message_id": str(event.message_id)},
        "signal": json.loads(txn.proposal_json),
    }


async def _apply(adapter, store, txn, request, reply_to):
    cfg = settings()
    try:
        response = await m3_client.call(request, timeout=12)
    except Exception as exc:
        logger.error("SIGNAL outcome indeterminate: %s", exc)
        store.indeterminate(txn.id, "transport_or_verification")
        await adapter.send(txn.chat_id, "Approval was received, but canonical application could not be verified. The request is indeterminate and no success is being claimed.", reply_to=reply_to)
        return
    if response["status"] == "APPLIED":
        receipt = response["process_receipt"]
        store.applied(txn.id, response["event_id"], receipt["receipt_sha256"])
        await adapter.send(txn.chat_id, f"Applied canonically on M3. SIGNAL event {response['event_id']} was processed and receipt {receipt['receipt_id']} was verified.", reply_to=reply_to)
    else:
        code = (response.get("error") or {}).get("code", "m3_rejected")
        store.rejected(txn.id, code)
        await adapter.send(txn.chat_id, f"M3 rejected the SIGNAL request ({code}). Nothing was applied.", reply_to=reply_to)


async def reconcile(adapter, *, all_inflight=False):
    cfg = settings()
    if not cfg.get("enabled"):
        return
    store = _store(cfg)
    cutoff = float("inf") if all_inflight else time.time() - 30
    for txn in store.stale_inflight(cutoff):
        if not txn.request_json:
            store.indeterminate(txn.id, "missing_request_record")
            continue
        if txn.state == "PROCESSING":
            store.indeterminate(txn.id, "restart_recovery")
        await _apply(adapter, store, store.get(txn.id), json.loads(txn.request_json), None)


async def intercept(adapter, event):
    if event.text not in ("Y", "y"):
        return False
    cfg = settings()
    if not cfg.get("enabled") or str(event.source.user_id) != str(cfg.get("bill_user_id")) or str(event.source.chat_id) != str(cfg.get("chat_id")):
        return False
    await reconcile(adapter)
    store = _store(cfg)
    if event.platform_update_id is None or store.update_seen(event.platform_update_id):
        await adapter.send(event.source.chat_id, "That Telegram approval update was invalid or already consumed. Nothing was applied.", reply_to=event.message_id)
        return True
    eligible = store.eligible(event.source.user_id, event.source.chat_id)
    if len(eligible) != 1:
        store.consume_unbound(event.platform_update_id)
        await adapter.send(event.source.chat_id, "No single unexpired SIGNAL proposal was available. Nothing was applied.", reply_to=event.message_id)
        return True
    txn = eligible[0]
    request_id = str(uuid.uuid4())
    request = _request(txn, event, request_id)
    if not store.claim(txn.id, event.platform_update_id, event.message_id, request_id, json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False), event.reply_to_message_id):
        await adapter.send(event.source.chat_id, "That approval was invalid or already consumed. Nothing was applied.", reply_to=event.message_id)
        return True
    await _apply(adapter, store, store.get(txn.id), request, event.message_id)
    return True

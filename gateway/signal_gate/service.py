import json
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home
from gateway.signal_gate import m3_client, schema
from gateway.signal_gate.store import Store

logger = logging.getLogger(__name__)
TODAY_GOALS_PREFIX = "Set today’s goals:"
_LIST_MARKER = re.compile(r"^[ \t]*(?:\d+[.)]|[-•])[ \t]*")


class GovernedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prepared:
    proposal_id: str
    store: Store
    display_text: str


def settings():
    gateway = load_config_readonly().get("gateway", {})
    raw = gateway.get("signal_gate", {}) if isinstance(gateway, dict) else {}
    if not isinstance(raw, dict):
        return {"enabled": False}
    return raw


def _store(cfg):
    path = cfg.get("database") or str(get_hermes_home() / "signal_gate.db")
    return Store(Path(path).expanduser())


def is_governed_lane(source, config=None) -> bool:
    cfg = config if config is not None else load_config_readonly()
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    gate = gateway.get("signal_gate", {}) if isinstance(gateway, dict) else {}
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    return (
        isinstance(gate, dict)
        and gate.get("enabled") is True
        and platform == "telegram"
        and getattr(source, "chat_type", None) == "dm"
        and str(getattr(source, "user_id", None)) == str(gate.get("bill_user_id"))
        and str(getattr(source, "chat_id", None)) == str(gate.get("chat_id"))
    )


def _parse_today_goals(text):
    if not isinstance(text, str) or not text[:len(TODAY_GOALS_PREFIX)].casefold() == TODAY_GOALS_PREFIX.casefold():
        return None
    remainder = text[len(TODAY_GOALS_PREFIX):]
    if remainder.startswith("\r\n"):
        remainder = remainder[2:]
    elif remainder.startswith("\n"):
        remainder = remainder[1:]
    else:
        return ()
    lines = remainder.splitlines()
    goals = tuple(_LIST_MARKER.sub("", line, count=1) for line in lines)
    if any(not goal.strip() for goal in goals):
        return ()
    return goals


def _today_has_goals():
    return _today_state()["has_goals"]


def _today_state():
    from plugins.dashboard_status.client import fetch_state

    upstream, _ = fetch_state()
    current = upstream.get("signal") if isinstance(upstream, dict) else None
    today = datetime.now(ZoneInfo("Australia/Sydney")).date().isoformat()
    has_goals = (
        isinstance(current, dict)
        and current.get("date") == today
        and isinstance(current.get("actions"), list)
        and bool(current["actions"])
    )
    return {
        "target_date": today,
        "has_goals": has_goals,
        "state_sha256": schema.sha256(json.dumps(
            current, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )),
    }


async def intercept_today_goals(event):
    """Handle the one exact Bill-lane command before model dispatch."""
    goals = _parse_today_goals(getattr(event, "text", None))
    if goals is None:
        return None
    try:
        cfg = settings()
    except Exception:
        return "I couldn’t safely prepare today’s goals, so nothing was changed."
    if not is_governed_lane(
        event.source, {"gateway": {"signal_gate": cfg}}
    ):
        return None
    if not goals:
        return "Please send at least one goal, with one non-empty goal per line."
    if len(goals) > 3:
        return "Please choose your top three goals and resend them."
    proposal = schema.Proposal(
        tuple({"text": goal, "mission": "billos"} for goal in goals),
        today_goals=True,
    )
    try:
        schema.parse_display(schema.render_display(proposal))
    except schema.ValidationError:
        return "Please send one to three goals, with one non-empty goal per line."
    from gateway.raw_capture_gate import service as raw_capture
    capture = await raw_capture.capture_result(event, event.text)
    if capture is None:
        return "I couldn’t durably capture that request, so no goals proposal was created."

    canonical = schema.canonical_json(proposal)
    proposal_sha256 = schema.sha256(canonical)
    source_sha256 = hashlib.sha256(event.text.encode("utf-8")).hexdigest()
    store = _store(cfg)
    replay = store.today_goals_replay(
        event.platform_update_id, event.message_id,
        source_sha256, proposal_sha256,
    )
    if replay == "identical":
        return ""
    if replay == "conflict":
        return "That Telegram update conflicts with the existing goals proposal. Nothing was changed."
    try:
        state = _today_state()
    except Exception:
        return "I couldn’t verify whether today’s goals are already set, so nothing was changed."

    capture_json = json.dumps({
        "capture_id": capture.capture_id,
        "event_id": capture.event_id,
        "content_sha256": capture.content_sha256,
        "receipt_sha256": capture.receipt_sha256,
        "source_update_id": capture.source_update_id,
        "source_message_id": capture.source_message_id,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    staged = store.stage_today_goals(
        source_update_id=event.platform_update_id,
        source_message_id=event.message_id,
        source_sha256=source_sha256,
        proposal_sha256=proposal_sha256,
        capture_json=capture_json,
        target_date=state["target_date"],
        expected_state_sha256=state["state_sha256"],
        replacement=state["has_goals"],
    )
    if staged == "conflict":
        return "That Telegram update conflicts with the existing goals proposal. Nothing was changed."
    if staged == "identical":
        return ""
    proposal = schema.Proposal(
        proposal.actions, replacement=state["has_goals"], today_goals=True,
    )
    return schema.render_display(proposal)


def prepare(adapter, event, text, *, has_attachments=False):
    cfg = settings()
    if not cfg.get("enabled"):
        return None
    proposal = schema.parse_display(text)
    if proposal is None:
        if (
            "```signal" in (text or "")
            or "approve canonical application" in (text or "").lower()
            or (text or "").startswith("I’ve prepared these as today’s goals:")
        ):
            raise GovernedError("invalid governed SIGNAL envelope")
        return None
    platform = getattr(
        getattr(adapter, "platform", None),
        "value",
        getattr(adapter, "platform", None),
    )
    if platform != "telegram":
        raise GovernedError(
            "governed SIGNAL requires Telegram"
        )
    if has_attachments:
        raise GovernedError(
            "governed SIGNAL requires one text-only Telegram message"
        )
    if getattr(event.source, "chat_type", None) != "dm":
        raise GovernedError("governed SIGNAL requires a private Telegram chat")
    if str(event.source.user_id) != str(cfg.get("bill_user_id")) or str(event.source.chat_id) != str(cfg.get("chat_id")):
        raise GovernedError("governed SIGNAL is restricted to the configured private lane")
    canonical = schema.canonical_json(proposal)
    display_text = schema.render_display(proposal)
    store = _store(cfg)
    kwargs = {
        "bill_user_id": event.source.user_id,
        "chat_id": event.source.chat_id,
        "source_update_id": event.platform_update_id,
        "display_sha256": schema.sha256(display_text),
        "proposal_sha256": schema.sha256(canonical),
        "proposal_json": canonical,
        "ttl": 1800,
    }
    if proposal.today_goals:
        pid = store.prepare_today_goals(**kwargs)
        if pid is None:
            raise GovernedError("today goals proposal was not durably staged")
    else:
        pid = store.prepare(
            source_message_id=event.message_id,
            **kwargs,
        )
    return Prepared(pid, store, display_text)


def finalize(prepared, result):
    ids = [str(x) for x in ([result.message_id] + list(getattr(result, "continuation_message_ids", ()) or ())) if x]
    raw = getattr(result, "raw_response", None) or {}
    ids += [str(x) for x in raw.get("message_ids", ()) if x and str(x) not in ids]
    if result.success and len(ids) == 1 and prepared.store.arm(prepared.proposal_id, ids[0]):
        return True
    prepared.store.delivery_failed(prepared.proposal_id)
    return False


def _request(txn, event, request_id, store=None):
    request = {
        "version": 1, "operation": "SIGNAL_SET", "request_id": request_id,
        "proposal_id": txn.id, "proposal_sha256": txn.proposal_sha256,
        "approved_by": {"platform": "telegram", "user_id": str(event.source.user_id),
            "chat_id": str(event.source.chat_id), "proposal_message_id": txn.proposal_message_id,
            "approval_update_id": str(event.platform_update_id), "approval_message_id": str(event.message_id)},
        "signal": json.loads(txn.proposal_json),
    }
    goals = store.today_goals(txn.id) if store is not None else None
    if goals:
        request["source_capture"] = json.loads(goals["capture_json"])
        request["today_goals"] = {
            "target_date": goals["target_date"],
            "expected_state_sha256": goals["expected_state_sha256"],
            "replacement": bool(goals["replacement"]),
        }
    return request


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
        try:
            committed = store.applied(txn.id, response["event_id"], receipt["receipt_sha256"])
        except Exception as exc:
            logger.error("SIGNAL local APPLIED persistence failed: %s", exc)
            committed = False
        if not committed:
            try:
                current = store.get(txn.id)
            except Exception:
                current = None
            if current and current.state == "APPLIED":
                return
            try:
                store.indeterminate(txn.id, "local_applied_persistence")
            except Exception:
                pass
            await adapter.send(txn.chat_id, "M3 returned a verified receipt, but Williams could not durably record APPLIED. The request remains indeterminate and no ordinary APPLIED success is being announced.", reply_to=reply_to)
            return
        await adapter.send(
            txn.chat_id,
            "Done — today’s goals are set." if store.today_goals(txn.id) else
            "Applied canonically on M3. The verified SIGNAL receipt was recorded.",
            reply_to=reply_to,
        )
    else:
        error = response.get("error") or {}
        code = error.get("code", "m3_rejected")
        detail = error.get("message")
        # Retain M3's exact rejection code and message in the durable SIGNAL
        # transaction and the local audit log for diagnosis. The user-facing
        # reply stays plain and free of codes, IDs, JSON, hashes and receipts.
        logger.error(
            "SIGNAL rejected by M3 (proposal %s): code=%s detail=%s",
            txn.id, code, detail,
        )
        store.rejected(txn.id, code, detail)
        if store.today_goals(txn.id):
            message = "I couldn’t update today’s goals. Nothing was changed."
        else:
            message = "I couldn’t apply that on M3. Nothing was changed."
        await adapter.send(txn.chat_id, message, reply_to=reply_to)


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
    if getattr(event.source, "chat_type", None) != "dm":
        await adapter.send(event.source.chat_id, "SIGNAL approval is restricted to Bill's configured private Telegram chat. Nothing was applied.", reply_to=event.message_id)
        return True
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
    request = _request(txn, event, request_id, store)
    if not store.claim(txn.id, event.platform_update_id, event.message_id, request_id, json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False), event.reply_to_message_id):
        await adapter.send(event.source.chat_id, "That approval was invalid or already consumed. Nothing was applied.", reply_to=event.message_id)
        return True
    await _apply(adapter, store, store.get(txn.id), request, event.message_id)
    return True

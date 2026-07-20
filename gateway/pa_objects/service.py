"""PA object orchestration — recognise, resolve target, apply M3-first, confirm.

Runs on the Christine plane. Ordinary conversation always falls through: only an
explicit obligation/decision statement or an explicit transition command is ever
handled here. Every canonical effect is M3-first; Williams claims success only on
a verified canonical receipt. A replayed Telegram update never creates or
transitions a second object (local update-id ledger + M3 request idempotency).

Collaborator ``m3`` is injected so tests exercise the whole pipeline against a
fake with no real SSH round-trip.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from hermes_constants import get_hermes_home
from gateway.pa_objects import m3_client, parse, schema
from gateway.pa_objects.store import Store

logger = logging.getLogger(__name__)

INDETERMINATE_MSG = (
    "I received that, but I couldn't verify it was saved canonically, so I'm not "
    "claiming it was. Nothing is being treated as done."
)


def settings() -> dict:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    raw = gateway.get("pa_objects_gate", {}) if isinstance(gateway, dict) else {}
    return raw if isinstance(raw, dict) else {"enabled": False}


def _store(cfg) -> Store:
    path = cfg.get("database") or str(get_hermes_home() / "pa_objects" / "state.sqlite")
    return Store(Path(path).expanduser())


def _platform(source):
    return getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))


def is_lane(source, cfg) -> bool:
    return (
        isinstance(cfg, dict)
        and cfg.get("enabled") is True
        and _platform(source) == "telegram"
        and getattr(source, "chat_type", None) == "dm"
        and str(getattr(source, "user_id", None)) == str(cfg.get("bill_user_id"))
        and str(getattr(source, "chat_id", None)) == str(cfg.get("chat_id"))
    )


def _provenance(event) -> dict:
    return {
        "telegram_update_id": str(event.platform_update_id),
        "telegram_user_id": str(event.source.user_id),
        "telegram_chat_id": str(event.source.chat_id),
        "source_message_id": str(event.message_id),
    }


def _now_sydney_rfc3339() -> str:
    return datetime.now(parse.SYDNEY).isoformat()


def _content_sha(request: dict) -> str:
    # Identity stable across replays of the same update: excludes request_id and
    # the approval timestamp, includes kind/action/object (object carries the
    # Telegram provenance, which is what makes it unique per update).
    return schema.canonical_hash({
        "kind": request["kind"], "action": request["action"], "object": request["object"],
    })


# --- confirmations (normal language, from the verified canonical projection) --

def _quote(text: str) -> str:
    return "“" + text + "”"


def _confirm(kind: str, action: str, obj: dict) -> str:
    wording = obj.get("wording", "")
    if action == "create" and kind == "obligation":
        msg = f"Recorded as an obligation: {_quote(wording)}."
        if obj.get("due_date"):
            msg += f" Due {parse.human_date(obj['due_date'])}."
        return msg
    if action == "create" and kind == "needs_bill":
        return f"Recorded as a decision for you to make: {_quote(wording)}."
    if action == "set_waiting":
        who = f" on {obj['person']}" if obj.get("person") else ""
        return f"Done — that obligation is now waiting{who}: {_quote(wording)}."
    if action == "resume":
        return f"Done — that obligation is active again: {_quote(wording)}."
    if action == "complete":
        return f"Done — that obligation is complete: {_quote(wording)}."
    if action == "cancel" and kind == "obligation":
        return f"Done — that obligation is cancelled: {_quote(wording)}."
    if action == "resolve":
        return f"Done — decision resolved: {_quote(obj.get('decision') or '')} (for {_quote(wording)})."
    if action == "cancel" and kind == "needs_bill":
        return f"Done — that decision is cancelled: {_quote(wording)}."
    return "Done."


# --- main entry --------------------------------------------------------------

async def intercept(event, *, m3=m3_client):
    """Handle an obligation/needs_bill creation or transition, or return None to
    leave ordinary conversation untouched."""
    cfg = settings()
    if not is_lane(event.source, cfg):
        return None
    text = event.text if isinstance(event.text, str) else ""
    if not text.strip():
        return None

    update_id = getattr(event, "platform_update_id", None)
    store = _store(cfg)

    # An already-applied update is answered from cache — no M3 call, no duplicate.
    if update_id is not None:
        prior = store.get(update_id)
        if prior is not None and prior.state == "APPLIED":
            return prior.reply_text

    transition = parse.parse_transition(text)
    if transition is not None:
        return await _handle_transition(store, event, transition, m3=m3)

    creation = parse.parse_create(text)
    if creation is not None:
        return await _handle_create(store, event, creation, m3=m3)

    return None


async def _handle_create(store, event, creation, *, m3):
    update_id = getattr(event, "platform_update_id", None)
    if update_id is None or event.message_id is None:
        # Without stable Telegram identity we cannot guarantee replay-safety.
        return None
    try:
        wording = schema.preserve_wording(creation.wording)
        obj = {
            "wording": wording,
            "content_sha256": schema.sha256(wording),
            "provenance": _provenance(event),
        }
        if creation.kind == "obligation":
            obj = {
                "wording": wording,
                "content_sha256": schema.sha256(wording),
                "person": schema.clean_field(creation.person, "person"),
                "due_date": creation.due_date,
                "project": schema.clean_field(creation.project, "project"),
                "provenance": _provenance(event),
            }
    except schema.ValidationError:
        return None

    request = m3_client.build_create(
        request_id=str(uuid.uuid4()), kind=creation.kind, obj=obj,
        approval=m3_client.approval_block(_now_sydney_rfc3339()),
    )
    return await _apply(store, event, request, creation.kind, "create", m3=m3)


async def _handle_transition(store, event, transition, *, m3):
    update_id = getattr(event, "platform_update_id", None)
    if update_id is None or event.message_id is None:
        return None

    # A replay that is still in-flight reuses the stored request; only resolve a
    # fresh target when this update has not been seen.
    prior = store.get(update_id)
    if prior is None:
        try:
            target = await _resolve_target(transition, m3=m3)
        except m3_client.M3Error:
            return "I couldn't reach your items just now. Please try again in a moment."
        if target == "none":
            return _not_found(transition)
        if target == "many":
            return _which_one(transition)
        obj = {"object_id": target["object_id"], "base_version": target["object_version"]}
        if transition.action == "resolve":
            decision = schema.clean_field(transition.decision, "decision")
            if not decision:
                return "What's the decision? Tell me how to resolve it."
            obj["decision"] = decision
        request = m3_client.build_transition(
            request_id=str(uuid.uuid4()), kind=transition.kind, action=transition.action,
            obj=obj, approval=m3_client.approval_block(_now_sydney_rfc3339()),
        )
    else:
        request = None  # _apply will reuse the stored request bytes

    return await _apply(store, event, request, transition.kind, transition.action, m3=m3)


async def _resolve_target(transition, *, m3):
    listing = await m3.call(m3.build_list(request_id=str(uuid.uuid4()), kind=transition.kind))
    eligible_from = schema.ELIGIBLE_FROM[(transition.kind, transition.action)]
    candidates = [o for o in listing.get("objects", []) if o.get("status") in eligible_from]
    ref = (transition.ref or "").lower().strip()
    if ref:
        matched = [
            o for o in candidates
            if (o.get("person") or "").lower() == ref
            or ref in (o.get("wording") or "").lower()
            or ref in (o.get("person") or "").lower()
        ]
        candidates = matched
    if not candidates:
        return "none"
    if len(candidates) > 1:
        return "many"
    return candidates[0]


def _not_found(transition) -> str:
    noun = "obligation" if transition.kind == "obligation" else "decision"
    return f"I couldn't find an open {noun} matching that. Nothing was changed."


def _which_one(transition) -> str:
    noun = "obligation" if transition.kind == "obligation" else "decision"
    return f"You have more than one {noun} that could match — tell me a few words from the one you mean."


async def _apply(store, event, request, kind, action, *, m3):
    update_id = getattr(event, "platform_update_id", None)

    if request is not None:
        content_sha = _content_sha(request)
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        txn, identical = store.begin(
            update_id=update_id, request_id=request["request_id"],
            request_sha256=content_sha, request_json=request_json, kind=kind, action=action,
        )
        if not identical:
            return "I've already handled that message; nothing was changed."
        if txn.state == "APPLIED":
            return txn.reply_text
        send = json.loads(txn.request_json)
    else:
        txn = store.get(update_id)
        if txn is None:
            return None
        if txn.state == "APPLIED":
            return txn.reply_text
        send = json.loads(txn.request_json)

    try:
        response = await m3.call(send)
    except Exception as exc:
        logger.error("pa_object %s/%s indeterminate: %s", kind, action, exc)
        store.indeterminate(update_id, "transport_or_verification")
        return INDETERMINATE_MSG

    if response.get("status") != "APPLIED":
        error = response.get("error") or {}
        store.failed(update_id, error.get("code", "m3_rejected"))
        return "I couldn't record that. Nothing was changed."

    obj = response["object"]
    reply = _confirm(obj["kind"], response.get("action", action), obj)
    store.applied(update_id, obj["object_id"], reply)
    return reply

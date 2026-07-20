"""PA object orchestration — the bounded write behind Christine's model tool.

Christine's AI model owns the conversation: it interprets intent, asks natural
clarifying questions over as many turns as it needs, and only then decides to
record something. The functions here are the bounded canonical write it calls
through the ``pa_object`` tool — they do NOT interpret ordinary conversation and
are never run before the model. Every canonical effect is M3-first; success is
claimed only on a verified canonical receipt; a repeated identical write is
idempotent (content-keyed local ledger + M3 request ledger); there is no delete.

``tool_create`` / ``tool_transition`` are the model-facing entry points. The
older ``intercept`` remains only as the shared, well-tested write pipeline and is
no longer wired to run before the model.

Collaborator ``m3`` is injected so tests exercise the whole pipeline against a
fake with no real SSH round-trip.
"""

from __future__ import annotations

import json
import logging
import re
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
            msg += (f" Due {parse.human_date(obj['due_date'])}."
                    " Want me to set a reminder for it?")
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


# =============================================================================
# Model-facing tool API. Christine's model calls these AFTER it has understood
# the request (conversing/clarifying first as needed). Structured arguments only
# — no natural-language interpretation happens here.
# =============================================================================

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_due(due):
    """Return YYYY-MM-DD or None. Accepts an explicit date or a natural phrase
    ('Friday', 'tomorrow night'), resolved in Australia/Sydney. Never invents."""
    if due is None or (isinstance(due, str) and not due.strip()):
        return None
    if isinstance(due, str) and _ISO_DATE.match(due.strip()):
        try:
            datetime.strptime(due.strip(), "%Y-%m-%d")
            return due.strip()
        except ValueError:
            return None
    d = parse.resolve_due_date(str(due))
    return d.isoformat() if d else None


def _tool_provenance(cfg, session_id, idem):
    """Provenance for a model-tool write: Bill's configured lane plus a stable,
    session-scoped id. Honest traceability; not a real inbound Telegram update."""
    sid = (session_id or "na")[:48]
    return {
        "telegram_update_id": f"tool-{sid}-{idem[:12]}",
        "telegram_user_id": str(cfg.get("bill_user_id") or "bill"),
        "telegram_chat_id": str(cfg.get("chat_id") or "bill"),
        "source_message_id": f"tool-{sid}",
    }


async def _apply_tool(cfg, idem, request, kind, action, *, m3):
    """Shared M3-first apply for a tool write, idempotent on the content key."""
    store = _store(cfg)
    content_sha = _content_sha(request)
    request_json = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    txn, identical = store.begin(
        update_id=idem, request_id=request["request_id"],
        request_sha256=content_sha, request_json=request_json, kind=kind, action=action,
    )
    if not identical:
        return {"ok": False, "message": "A different write is already recorded for that exact request."}
    if txn.state == "APPLIED":
        return {"ok": True, "already": True, "object_id": txn.object_id, "message": txn.reply_text}
    send = json.loads(txn.request_json)
    try:
        response = await m3.call(send)
    except Exception as exc:
        logger.error("pa_object tool %s/%s indeterminate: %s", kind, action, exc)
        store.indeterminate(idem, "transport_or_verification")
        return {"ok": False, "message": INDETERMINATE_MSG}
    if response.get("status") != "APPLIED":
        error = response.get("error") or {}
        store.failed(idem, error.get("code", "m3_rejected"))
        return {"ok": False, "message": "I couldn't record that on M3. Nothing was changed.",
                "error": error.get("code", "m3_rejected")}
    obj = response["object"]
    reply = _confirm(obj["kind"], response.get("action", action), obj)
    store.applied(idem, obj["object_id"], reply)
    return {"ok": True, "object_id": obj["object_id"], "kind": obj["kind"],
            "status": obj["status"], "object_version": obj["object_version"], "message": reply}


async def tool_create(kind, wording, *, person=None, due=None, project=None,
                      session_id=None, m3=m3_client):
    cfg = settings()
    if cfg.get("enabled") is not True:
        return {"ok": False, "message": "PA objects are not enabled."}
    if kind not in schema.KINDS:
        return {"ok": False, "message": f"Unknown object kind {kind!r}."}
    try:
        w = schema.preserve_wording(wording)
    except schema.ValidationError:
        return {"ok": False, "message": "I need the wording before I can record it."}
    try:
        if kind == "obligation":
            obj = {
                "wording": w, "content_sha256": schema.sha256(w),
                "person": schema.clean_field(person, "person"),
                "due_date": _resolve_due(due),
                "project": schema.clean_field(project, "project"),
                "provenance": None,
            }
        else:
            obj = {"wording": w, "content_sha256": schema.sha256(w), "provenance": None}
    except schema.ValidationError:
        return {"ok": False, "message": "One of the details wasn't usable; nothing was recorded."}
    idem = schema.sha256(schema.canonical_json(
        {"k": kind, "a": "create", "w": w, "p": obj.get("person"),
         "d": obj.get("due_date"), "pr": obj.get("project"), "s": session_id}))
    obj["provenance"] = _tool_provenance(cfg, session_id, idem)
    request = m3_client.build_create(
        request_id=str(uuid.uuid4()), kind=kind, obj=obj,
        approval=m3_client.approval_block(_now_sydney_rfc3339()))
    return await _apply_tool(cfg, idem, request, kind, "create", m3=m3)


async def tool_transition(kind, action, *, reference=None, object_id=None,
                          decision=None, session_id=None, m3=m3_client):
    cfg = settings()
    if cfg.get("enabled") is not True:
        return {"ok": False, "message": "PA objects are not enabled."}
    if action not in schema.TRANSITIONS.get(kind, {}):
        return {"ok": False, "message": f"Unsupported action {action!r} for {kind}."}
    try:
        target = await _resolve_tool_target(kind, action, reference, object_id, m3=m3)
    except m3_client.M3Error:
        return {"ok": False, "message": "I couldn't reach your items just now. Please try again."}
    if target == "none":
        noun = "obligation" if kind == "obligation" else "decision"
        return {"ok": False, "message": f"I couldn't find an open {noun} matching that."}
    if target == "many":
        noun = "obligation" if kind == "obligation" else "decision"
        return {"ok": False, "message": f"More than one {noun} could match — which one?"}
    if target == "ineligible":
        return {"ok": False, "message": "That item isn't in a state this change applies to."}
    obj = {"object_id": target["object_id"], "base_version": target["object_version"]}
    if action == "resolve":
        d = schema.clean_field(decision, "decision")
        if not d:
            return {"ok": False, "message": "What's the decision? I need it to resolve this."}
        obj["decision"] = d
    idem = schema.sha256(schema.canonical_json(
        {"k": kind, "a": action, "id": obj["object_id"], "v": obj["base_version"],
         "dec": obj.get("decision"), "s": session_id}))
    request = m3_client.build_transition(
        request_id=str(uuid.uuid4()), kind=kind, action=action, obj=obj,
        approval=m3_client.approval_block(_now_sydney_rfc3339()))
    return await _apply_tool(cfg, idem, request, kind, action, m3=m3)


async def _resolve_tool_target(kind, action, reference, object_id, *, m3):
    eligible_from = schema.ELIGIBLE_FROM[(kind, action)]
    if object_id is not None:
        listing = await m3.call(m3.build_list(request_id=str(uuid.uuid4()), kind=kind, object_id=object_id))
        objs = listing.get("objects", [])
        if not objs:
            return "none"
        return objs[0] if objs[0].get("status") in eligible_from else "ineligible"
    listing = await m3.call(m3.build_list(request_id=str(uuid.uuid4()), kind=kind))
    candidates = [o for o in listing.get("objects", []) if o.get("status") in eligible_from]
    ref = (reference or "").lower().strip()
    if ref:
        candidates = [
            o for o in candidates
            if (o.get("person") or "").lower() == ref
            or ref in (o.get("wording") or "").lower()
            or ref in (o.get("person") or "").lower()
        ]
    if not candidates:
        return "none"
    if len(candidates) > 1:
        return "many"
    return candidates[0]

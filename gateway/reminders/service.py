"""Reminder orchestration — intake, confirmation, M3-first apply, reconciliation.

Runs on the Christine plane. Ordinary conversation always falls through: only an
explicit reminder request, an explicit management command, or a yes/no reply to a
single pending reminder proposal is ever handled here.

The mutation pipeline is deliberately identical for every action:

    recognise -> propose (echo absolute date/time/wording) -> Bill confirms
    -> M3 write FIRST -> verify canonical receipt -> local scheduler effect
    -> verify local job -> "Done."

Nothing canonical is written before Bill confirms. If M3 fails, there is no local
job and no "Done." If M3 succeeds but the local job cannot be created/verified,
success is not claimed and the canonical state is preserved for restart
reconciliation.

Collaborators (``m3``, ``bridge``, ``cron``) are injected so tests exercise the
whole pipeline against fakes — no real M3 write, scheduler job, or Telegram send.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from gateway.reminders import m3_client, parse, schema, scheduler_bridge
from gateway.reminders.store import Store

logger = logging.getLogger(__name__)

_AFFIRM = frozenset({
    "y", "yes", "yes please", "yep", "yeah", "yup", "ya", "yes do it",
    "confirm", "confirmed", "do that", "do it", "go ahead", "correct",
    "ok", "okay", "sure", "please do", "affirmative", "approved",
})
_REJECT = frozenset({
    "n", "no", "nope", "no thanks", "no thank you", "cancel", "cancelled",
    "leave it", "forget it", "dont", "do not", "never mind", "nevermind", "stop",
})
_AMBIGUOUS = frozenset({
    "maybe", "not sure", "unsure", "idk", "hmm", "perhaps", "i guess",
    "possibly", "not yet", "hold on", "wait", "later",
})

_REMIND_RE = re.compile(r"^\s*(?:hey\s+|please\s+)*(?:can you\s+|could you\s+|christine[, ]+)?remind me\b(.*)", re.IGNORECASE | re.DOTALL)
_VIEW_RE = re.compile(r"\b(what|which|show|list|see|view)\b.*\breminders?\b|\breminders?\b\s*\?|\bmy reminders?\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^\s*(?:please\s+)?(?:cancel|delete|remove|clear|drop)\b(.*?)\breminder\b(.*)$", re.IGNORECASE | re.DOTALL)
_CHANGE_RE = re.compile(r"^\s*(?:please\s+)?(?:change|move|reschedule|update|shift)\b(.*?)\breminder\b(.*)$", re.IGNORECASE | re.DOTALL)
_SNOOZE_RE = re.compile(r"^\s*(?:please\s+)?snooze\b(.*)$", re.IGNORECASE | re.DOTALL)


# --- configuration / lane ----------------------------------------------------

def settings() -> dict:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    raw = gateway.get("reminders_gate", {}) if isinstance(gateway, dict) else {}
    return raw if isinstance(raw, dict) else {"enabled": False}


def _store(cfg) -> Store:
    path = cfg.get("database") or str(get_hermes_home() / "reminders" / "state.sqlite")
    return Store(Path(path).expanduser())


def is_reminders_lane(source, cfg) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    return (
        isinstance(cfg, dict)
        and cfg.get("enabled") is True
        and platform == "telegram"
        and getattr(source, "chat_type", None) == "dm"
        and str(getattr(source, "user_id", None)) == str(cfg.get("bill_user_id"))
        and str(getattr(source, "chat_id", None)) == str(cfg.get("chat_id"))
    )


def _classify_reply(text) -> str:
    if not isinstance(text, str):
        return "other"
    n = text.strip().lower().replace("’", "'").replace("'", "")
    n = re.sub(r"[.,!?;:]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n in _AFFIRM:
        return "affirm"
    if n in _REJECT:
        return "reject"
    if n in _AMBIGUOUS:
        return "ambiguous"
    return "other"


# --- create intake -----------------------------------------------------------

def parse_create(utterance: str, *, now=None):
    """Split a 'remind me …' request into (text, Resolution). Returns one of:

    * ``("create", text, resolution)`` — a clear reminder with a resolvable time;
    * ``("clarify", reason, None)``     — a reminder whose time is missing/ambiguous;
    * ``None``                          — not a reminder-create request at all.
    """
    m = _REMIND_RE.match(utterance or "")
    if not m:
        return None
    rest = m.group(1).strip().lstrip(":,-").strip()
    if not rest:
        return ("clarify", "no_detail", None)

    clarify_reason = None

    # Form A: "remind me <time> to <text>"
    if re.search(r"\bto\b", rest, re.IGNORECASE):
        left, right = re.split(r"\bto\b", rest, maxsplit=1, flags=re.IGNORECASE)
        left, right = left.strip(), right.strip()
        if left and right:
            res = parse.resolve(left, now=now)
            if res.ok:
                text = _strip_trailing_time(right, now=now)
                return ("create", text or right, res)
            if res.needs_clarification and res.reason == "ambiguous_meridiem":
                clarify_reason = "ambiguous_meridiem"

    # Form B: "remind me [to] <text> <time>" — find the resolvable trailing time.
    body = re.sub(r"^to\s+", "", rest, flags=re.IGNORECASE).strip()
    tokens = body.split()
    for i in range(len(tokens)):
        cand = " ".join(tokens[i:])
        res = parse.resolve(cand, now=now)
        if res.ok:
            text = " ".join(tokens[:i]).strip(" ,")
            if text:
                return ("create", text, res)
            return ("clarify", "no_text", None)
        if res.needs_clarification and res.reason == "ambiguous_meridiem" and clarify_reason is None:
            clarify_reason = "ambiguous_meridiem"

    return ("clarify", clarify_reason or "no_time", None)


def _strip_trailing_time(text: str, *, now=None) -> str:
    """Best-effort removal of a trailing time phrase from reminder wording."""
    tokens = text.split()
    for i in range(len(tokens)):
        if parse.resolve(" ".join(tokens[i:]), now=now).ok:
            head = " ".join(tokens[:i]).strip(" ,")
            if head:
                return head
    return text


# --- proposal staging --------------------------------------------------------

def _stage(store, event, proposal: schema.ReminderProposal, *, ttl=1800) -> str:
    # Store exactly what _build_request needs at confirm time: the action and the
    # per-action reminder object (create's object already embeds provenance).
    proposal_json = json.dumps({
        "action": proposal.action,
        "reminder": proposal.reminder_object(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    pid = store.prepare(
        action=proposal.action,
        bill_user_id=event.source.user_id,
        chat_id=event.source.chat_id,
        reminder_id=proposal.reminder_id,
        base_version=proposal.base_version,
        source_update_id=event.platform_update_id,
        proposal_sha256=proposal.local_sha256(),
        proposal_json=proposal_json,
        ttl=ttl,
    )
    store.arm(pid, "")
    return pid


def _lane_target(cfg) -> dict:
    """The reminder delivery target is the configured Bill Telegram lane — an
    execution detail owned by Williams, not stored on M3."""
    return {"platform": "telegram", "chat_id": str(cfg.get("chat_id"))}


def _provenance(event) -> dict:
    return {
        "telegram_update_id": str(event.platform_update_id),
        "telegram_user_id": str(event.source.user_id),
        "telegram_chat_id": str(event.source.chat_id),
        "source_message_id": str(event.message_id),
    }


def _confirm_create(text: str, dt) -> str:
    return (f"I'll remind you to {text} on {parse.human(dt)}. "
            f"Reply yes to confirm, or no to cancel.")


# --- main entry --------------------------------------------------------------

async def intercept(event, *, m3=m3_client, bridge=scheduler_bridge, cron=None):
    """Handle a reminder request, management command, or confirmation reply.

    Returns a reply string to send (the raw-capture/thoughts hook convention in
    run.py), or ``None`` to leave ordinary conversation untouched.
    """
    cfg = settings()
    if not is_reminders_lane(event.source, cfg):
        return None

    store = _store(cfg)
    text = event.text if isinstance(event.text, str) else ""

    # 1) Confirmation reply to a single pending proposal.
    kind = _classify_reply(text)
    if kind in ("affirm", "reject", "ambiguous"):
        return await _handle_reply(store, event, kind, m3=m3, bridge=bridge, cron=cron)

    # 2) Explicit management commands (read-only until confirmed).
    if _VIEW_RE.search(text) and not _REMIND_RE.match(text):
        return await _view(store, event, m3=m3)
    cancel_m = _CANCEL_RE.match(text)
    if cancel_m:
        return await _propose_cancel(store, event, cancel_m, m3=m3)
    change_m = _CHANGE_RE.match(text)
    if change_m:
        return await _propose_change(store, event, change_m, m3=m3)
    snooze_m = _SNOOZE_RE.match(text)
    if snooze_m:
        return await _propose_snooze(store, event, snooze_m, m3=m3)

    # 3) Natural reminder creation.
    parsed = parse_create(text)
    if parsed is None:
        return None
    verb, a, b = parsed
    if verb == "clarify":
        if a == "ambiguous_meridiem":
            return "Did you mean the morning or the evening? Tell me the exact time and I'll set it."
        if a in ("no_text",):
            return "What should the reminder say?"
        return "When would you like me to remind you? Tell me a date and time."
    # verb == "create"
    text_body, resolution = a, b
    try:
        wording = schema.clean_text(text_body)
    except schema.ValidationError:
        return "What should the reminder say?"
    # reminder_id and version are assigned canonically by M3 on create; Williams
    # does not invent them. Provenance is captured from the request message.
    proposal = schema.ReminderProposal(
        action="create", wording=wording, due_at=parse.to_rfc3339(resolution.dt),
        recurrence=None, reminder_id=None, base_version=None, provenance=_provenance(event),
    )
    _stage(store, event, proposal)
    return _confirm_create(wording, resolution.dt)


def _recover_inflight(store, *, all_inflight=False):
    """Cheap recovery of interrupted confirmations (no M3 call).

    A confirmation that was claimed (PROCESSING) but never reached a terminal M3
    outcome is marked INDETERMINATE so it is never silently treated as applied.
    """
    cutoff = float("inf") if all_inflight else time.time() - 30
    for txn in store.stale_inflight(cutoff):
        if not txn.request_json:
            store.indeterminate(txn.id, "missing_request_record")
        elif txn.state == "PROCESSING":
            store.indeterminate(txn.id, "restart_recovery")


async def _handle_reply(store, event, kind, *, m3, bridge, cron):
    _recover_inflight(store)
    eligible = store.eligible(event.source.user_id, event.source.chat_id)
    if len(eligible) != 1:
        # No single pending reminder proposal — a bare yes/no belongs to ordinary
        # conversation; do not hijack it.
        return None
    txn = eligible[0]

    if kind == "ambiguous":
        return "Shall I go ahead? Just say yes or no."

    if event.platform_update_id is None or store.update_seen(event.platform_update_id):
        return "That reply was already handled. Nothing was changed."

    if kind == "reject":
        store.consume_unbound(event.platform_update_id)
        store.supersede(txn.id)
        return "Okay, I won't. Nothing was changed."

    request_id = str(uuid.uuid4())
    request = _build_request(txn, event, request_id)
    if not store.claim(txn.id, event.platform_update_id, event.message_id, request_id,
                       json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)):
        return "That reply was already handled. Nothing was changed."
    return await _apply(store, store.get(txn.id), request, event, m3=m3, bridge=bridge, cron=cron)


def _now_sydney_rfc3339() -> str:
    from datetime import datetime
    return datetime.now(parse.SYDNEY).isoformat()


def _build_request(txn, event, request_id) -> dict:
    body = json.loads(txn.proposal_json)
    approval = m3_client.approval_block(_now_sydney_rfc3339())
    return m3_client.build_upsert(
        action=txn.action,
        request_id=request_id,
        reminder_object=body.get("reminder"),
        approval=approval,
    )


# --- apply (M3 first, then local, then Done) ---------------------------------

async def _apply(store, txn, request, event, *, m3, bridge, cron):
    cfg = settings()
    try:
        response = await m3.call(request)
    except Exception as exc:
        logger.error("reminder %s indeterminate: %s", txn.id, exc)
        store.indeterminate(txn.id, "transport_or_verification")
        return ("I received your confirmation, but I couldn't verify it was saved. "
                "I'm not claiming it's set — nothing was scheduled.")

    if response.get("status") != "APPLIED":
        error = response.get("error") or {}
        store.rejected(txn.id, error.get("code", "m3_rejected"), error.get("message"))
        return "I couldn't set that. Nothing was changed."

    reminder = response["reminder"]
    reminder_id = reminder["reminder_id"]
    version = reminder["reminder_version"]
    # The self-sealed response_sha256 is this operation's canonical receipt.
    store.applied(txn.id, reminder_id, response["response_sha256"])

    if txn.action == "cancel" or reminder.get("status") == "cancelled":
        existing = store.get_execution(reminder_id)
        try:
            bridge.remove_oneshot(existing.cron_job_id if existing else None, reminder_id, cron=cron)
        except Exception as exc:
            logger.error("reminder %s local cancel cleanup failed: %s", reminder_id, exc)
        store.delete_execution(reminder_id)
        return "Done. That reminder is cancelled."

    # Schedule the local one-shot from the canonical projection: the DST-correct
    # local instant (due_at_local) drives the clock; the target is the Bill lane.
    target = _lane_target(cfg)
    due_local = reminder["due_at_local"]
    wording = reminder["wording"]
    existing = store.get_execution(reminder_id)
    old_job = existing.cron_job_id if existing else None
    store.upsert_execution(
        reminder_id=reminder_id, version=version, status="scheduled", text=wording,
        due_rfc3339=due_local, timezone=reminder["timezone"],
        target_json=scheduler_bridge.target_json(target), cron_job_id=None,
    )
    try:
        if txn.action == "create":
            job_id = bridge.create_oneshot(reminder_id=reminder_id, version=version,
                                           due_rfc3339=due_local, target=target, cron=cron)
        else:
            job_id = bridge.replace_oneshot(reminder_id=reminder_id, version=version,
                                            due_rfc3339=due_local, target=target,
                                            old_cron_job_id=old_job, cron=cron)
        if not bridge.verify_linked(job_id, reminder_id, cron=cron):
            raise RuntimeError("local job verification failed")
        store.set_cron_job(reminder_id, job_id)
    except Exception as exc:
        logger.error("reminder %s saved on M3 but local scheduling failed: %s", reminder_id, exc)
        return ("I've saved your reminder, but I couldn't finish scheduling it locally just now. "
                "It's recorded and will be scheduled automatically shortly — I'm not claiming it's fully set yet.")

    return f"Done. I'll remind you on {parse.human(_dt(due_local))}."


def _dt(due_rfc3339):
    from datetime import datetime
    return datetime.fromisoformat(due_rfc3339)


# --- view / change / snooze / cancel proposals -------------------------------

async def _list_scheduled(m3):
    listing = await m3.call(m3.build_list(request_id=str(uuid.uuid4()), status="scheduled"))
    return [r for r in listing.get("reminders", []) if r.get("status") == "scheduled"]


async def _view(store, event, *, m3):
    try:
        scheduled = await _list_scheduled(m3)
    except Exception as exc:
        logger.error("reminder view failed: %s", exc)
        return "I couldn't reach your reminders just now. Please try again in a moment."
    if not scheduled:
        return "You have no reminders set."
    lines = ["Here are your reminders:"]
    for r in sorted(scheduled, key=lambda x: x.get("due_at_local", "")):
        lines.append(f"• {r.get('wording', '').strip()} — {parse.human(_dt(r['due_at_local']))}")
    return "\n".join(lines)


def _clean_ref(ref: str) -> str:
    """Strip filler so a reminder reference matches its wording ("the SignalPath
    reminder" / "it" / "about the trip" -> a substring of the reminder text)."""
    ref = re.sub(r"\b(the|my|a|an|reminder|reminders|about|to|for|that says?|it|that|please)\b",
                 " ", ref or "", flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", ref).strip()


def _match_one(scheduled, ref):
    ref = _clean_ref(ref).lower()
    if not ref:
        return scheduled if len(scheduled) == 1 else []
    return [r for r in scheduled if ref in (r.get("wording") or "").lower()]


async def _propose_cancel(store, event, match, *, m3):
    ref = (match.group(1) + " " + match.group(2)).strip()
    try:
        scheduled = await _list_scheduled(m3)
    except Exception:
        return "I couldn't reach your reminders just now. Please try again in a moment."
    hits = _match_one(scheduled, ref)
    if not hits:
        return "I couldn't find a reminder matching that."
    if len(hits) > 1:
        return "You have more than one reminder matching that. Which one — tell me a few words from it?"
    r = hits[0]
    proposal = schema.ReminderProposal(
        action="cancel", wording=r.get("wording", ""), due_at=None,
        reminder_id=r["reminder_id"], base_version=r.get("reminder_version"),
    )
    _stage(store, event, proposal)
    return f"Cancel the reminder to {r.get('wording','').strip()} (due {parse.human(_dt(r['due_at_local']))})? Reply yes or no."


async def _propose_change(store, event, match, *, m3):
    ref = match.group(1).strip()
    tail = match.group(2).strip()
    time_part = re.sub(r"^\s*to\s+", "", tail, flags=re.IGNORECASE).strip()
    res = parse.resolve(time_part)
    if not res.ok:
        if res.needs_clarification and res.reason == "ambiguous_meridiem":
            return "Did you mean the morning or the evening? Tell me the exact new time."
        return "What time should I change it to?"
    try:
        scheduled = await _list_scheduled(m3)
    except Exception:
        return "I couldn't reach your reminders just now. Please try again in a moment."
    hits = _match_one(scheduled, ref)
    if not hits:
        return "I couldn't find a reminder matching that."
    if len(hits) > 1:
        return "You have more than one reminder matching that. Which one — tell me a few words from it?"
    r = hits[0]
    proposal = schema.ReminderProposal(
        action="change", wording=r.get("wording", ""), due_at=parse.to_rfc3339(res.dt),
        recurrence=r.get("recurrence"), reminder_id=r["reminder_id"],
        base_version=r.get("reminder_version"),
    )
    _stage(store, event, proposal)
    return f"Change the reminder to {r.get('wording','').strip()} to {parse.human(res.dt)}? Reply yes or no."


async def _propose_snooze(store, event, match, *, m3):
    tail = match.group(1).strip()
    ref = ""
    # "snooze <ref> for <duration>" / "snooze for <duration>" / "snooze <duration>"
    fm = re.search(r"\bfor\b(.*)$", tail, re.IGNORECASE)
    if fm:
        ref = tail[:fm.start()].strip()
        dur = fm.group(1).strip()
    else:
        dur = tail
    dur = re.sub(r"^(it|that|the reminder)\b", "", dur, flags=re.IGNORECASE).strip()
    res = parse.resolve("in " + dur if not re.search(r"\b(in|at|tomorrow|tonight|on)\b", dur, re.IGNORECASE) else dur)
    if not res.ok:
        return "How long should I snooze it — for example, 'for 10 minutes'?"
    try:
        scheduled = await _list_scheduled(m3)
    except Exception:
        return "I couldn't reach your reminders just now. Please try again in a moment."
    hits = _match_one(scheduled, ref)
    if not hits:
        return "I couldn't find a reminder to snooze."
    if len(hits) > 1:
        return "You have more than one reminder. Which one should I snooze — tell me a few words from it?"
    r = hits[0]
    proposal = schema.ReminderProposal(
        action="snooze", wording=r.get("wording", ""), due_at=parse.to_rfc3339(res.dt),
        reminder_id=r["reminder_id"], base_version=r.get("reminder_version"),
    )
    _stage(store, event, proposal)
    return f"Snooze the reminder to {r.get('wording','').strip()} until {parse.human(res.dt)}? Reply yes or no."


# --- reconciliation (restart rebuild + inflight recovery) --------------------

async def reconcile(*, m3=m3_client, bridge=scheduler_bridge, cron=None,
                    all_inflight=False):
    """Make Williams' local scheduler state match canonical M3.

    * finish any interrupted confirmation (PROCESSING/INDETERMINATE);
    * for every scheduled canonical reminder, ensure exactly one linked local
      one-shot job exists (recreate missing ones);
    * remove local jobs for reminders M3 no longer has scheduled (cancelled,
      delivered, superseded).

    M3 is the source of truth; a network failure aborts safely (no destructive
    local change on an unverifiable read).
    """
    cfg = settings()
    if not cfg.get("enabled"):
        return
    store = _store(cfg)

    # 1) Interrupted confirmations.
    _recover_inflight(store, all_inflight=all_inflight)

    # 2) Canonical vs local jobs.
    try:
        scheduled = await _list_scheduled(m3)
    except Exception as exc:
        logger.error("reminder reconcile: M3 list unavailable: %s", exc)
        return
    canonical_ids = set()
    target = _lane_target(cfg)
    for r in scheduled:
        reminder_id = r.get("reminder_id")
        due_local = r.get("due_at_local")
        version = r.get("reminder_version")
        if not reminder_id or not due_local or not isinstance(version, int):
            continue
        canonical_ids.add(reminder_id)
        existing = store.get_execution(reminder_id)
        store.upsert_execution(
            reminder_id=reminder_id, version=version, status="scheduled",
            text=r.get("wording", ""), due_rfc3339=due_local,
            timezone=r.get("timezone", "Australia/Sydney"),
            target_json=scheduler_bridge.target_json(target),
            cron_job_id=existing.cron_job_id if existing else None,
        )
        if existing and existing.cron_job_id and existing.version == version \
                and bridge.verify_linked(existing.cron_job_id, reminder_id, cron=cron):
            continue  # correct job already present — do not duplicate
        try:
            job_id = bridge.replace_oneshot(
                reminder_id=reminder_id, version=version, due_rfc3339=due_local,
                target=target, old_cron_job_id=existing.cron_job_id if existing else None,
                cron=cron,
            )
            store.set_cron_job(reminder_id, job_id)
        except Exception as exc:
            logger.error("reminder reconcile: could not schedule %s: %s", reminder_id, exc)

    # 3) Local jobs whose reminder is no longer scheduled canonically.
    for execution in store.list_executions():
        if execution.reminder_id not in canonical_ids:
            try:
                bridge.remove_oneshot(execution.cron_job_id, execution.reminder_id, cron=cron)
            except Exception:
                pass
            store.delete_execution(execution.reminder_id)

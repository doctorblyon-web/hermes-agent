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
from gateway.signal_gate import goals_mode, m3_client, schema
from gateway.signal_gate.store import Store

logger = logging.getLogger(__name__)
TODAY_GOALS_PREFIX = "Set today’s goals:"
_LIST_MARKER = re.compile(r"^[ \t]*(?:\d+[.)]|[-•])[ \t]*")

# Natural approval / rejection vocabulary for the goals confirmation reply.
# Matched case-insensitively with surrounding whitespace and trailing
# punctuation tolerated; only used to approve/reject a single pending proposal.
_AFFIRM = frozenset({
    "y", "yes", "yes please", "yep", "yeah", "yup", "ya", "yes do it",
    "confirm", "confirmed", "confirm please", "do that", "do it", "go ahead",
    "correct", "ok", "okay", "sure", "please do", "affirmative", "approved",
})
_REJECT = frozenset({
    "n", "no", "nope", "no thanks", "no thank you", "cancel", "cancelled",
    "leave them", "leave it", "leave them as they are", "keep them",
    "keep it", "keep the current goals", "keep current goals",
    "keep the current", "keep the current ones", "dont change them",
    "do not change them", "dont change", "dont change it", "no dont",
})
_AMBIGUOUS = frozenset({
    "maybe", "not sure", "unsure", "idk", "i dont know", "hmm", "perhaps",
    "i guess", "dunno", "possibly", "not yet", "hold on", "wait", "later",
})


def _classify_reply(text):
    if not isinstance(text, str):
        return "other"
    # Normalise for tolerant matching: lowercase, drop apostrophes, treat any
    # punctuation as a separator, and collapse surrounding/internal whitespace.
    normalized = text.strip().lower().replace("’", "'").replace("'", "")
    normalized = re.sub(r"[.,!?;:]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if normalized in _AFFIRM:
        return "affirm"
    if normalized in _REJECT:
        return "reject"
    if normalized in _AMBIGUOUS:
        return "ambiguous"
    return "other"


class GovernedError(RuntimeError):
    pass


# A fenced ```signal … ``` block, plus the plain-text SIGNAL/goals boilerplate lines
# that ``prepare`` treats as an envelope. Used only to salvage a reply when an envelope
# fails validation, so the model's surrounding conversational content is never lost.
_SIGNAL_BLOCK = re.compile(r"```signal\b.*?```", re.DOTALL | re.IGNORECASE)
_SIGNAL_BOILERPLATE = re.compile(
    r"(?im)^\s*(?:"
    r"captured\.?\s*$"
    r"|captured as (?:a proposal|today[’']?s goals proposal)\..*$"
    r"|i[’']?ve prepared these as today[’']?s goals:\s*$"
    r"|.*approve canonical application.*$"
    r")"
)


def strip_signal_envelope(text: str) -> str:
    """Return *text* with any SIGNAL/goals envelope fragments removed.

    Removes the fenced ``signal`` block and the recognised SIGNAL boilerplate lines,
    leaving whatever genuine conversational content the model wrote around them. A
    malformed envelope must never discard that content (e.g. a reflected obligation or
    a separate request in the same message). Returns "" if nothing else remains.
    """
    if not isinstance(text, str) or not text:
        return ""
    without_block = _SIGNAL_BLOCK.sub("", text)
    without_boilerplate = _SIGNAL_BOILERPLATE.sub("", without_block)
    # Collapse the blank lines the removals leave behind.
    return re.sub(r"\n{3,}", "\n\n", without_boilerplate).strip()


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


# --- explicit Goals-mode entry (a small conversation-state gate) -------------

# Explicit natural ways Bill opens Goals. Deliberately narrow: each names "goals"
# (or "priorities") for "today". None of these match ordinary "I need to…",
# "I've got to…", "I need to remember…" or "make sure I…" statements.
_GOALS_INITIATION = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|so\s+)?(?:christine[,:]?\s*)?"
    r"(?:"
    r"(?:these|here)\s+are\s+my\s+(?:top\s+\w+\s+)?(?:goals|priorities)\s+for\s+today"
    r"|my\s+(?:goals|priorities)\s+for\s+today\s+are"
    r"|today'?s\s+(?:goals|priorities)\s+are"
    r"|set\s+(?:my\s+|today'?s\s+)?(?:goals|priorities)(?:\s+for\s+today)?"
    r"|let'?s\s+(?:do|set|sort\s+out)\s+(?:today'?s\s+)?(?:goals|priorities)(?:\s+for\s+today)?"
    r"|let\s+us\s+(?:do|set)\s+(?:today'?s\s+)?(?:goals|priorities)"
    r"|time\s+for\s+today'?s\s+(?:goals|priorities)"
    r")"
    r"(?P<rest>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)

# Messages that must NEVER be captured as goals, even while Goals mode is armed:
# they belong to obligation / reminder / thought / Needs Bill / ordinary routing.
_OTHER_INTENT = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|so\s+)?(?:christine[,:]?\s*)?"
    r"(?:"
    r"i\s+(?:need|have|must|ought|(?:'ve|ve|have)\s+got|got|gotta)\s+to\b"
    r"|i\s+need\s+to\s+remember\b"
    r"|make\s+sure\b"
    r"|don'?t\s+let\s+me\b"
    r"|remind\s+me\b"
    r"|save\s+this\s+as\b"
    r"|remember\s+(?:this|to)\b"
    r"|i\s+need\s+to\s+decide\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_other_intent(text) -> bool:
    return isinstance(text, str) and _OTHER_INTENT.match(text) is not None


def _parse_goal_lines(text):
    """Split a free block into 1..n goal strings, or return () if none.

    Accepts newline-separated lines, or a single line of numbered / comma /
    'and'-separated items. List markers are stripped; nothing is rewritten.
    """
    if not isinstance(text, str) or not text.strip():
        return ()
    body = text.strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) <= 1:
        single = lines[0] if lines else body
        # numbered items on one line: "1) a 2) b 3) c"
        parts = re.split(r"\s*\b\d+[.)]\s*", single)
        parts = [p for p in parts if p.strip()]
        if len(parts) <= 1:
            parts = re.split(r"\s*,\s*|\s+and\s+", single)
        lines = parts
    goals = tuple(_LIST_MARKER.sub("", ln, count=1).strip() for ln in lines)
    # A real goal has actual content — a stray "." or "-" is not a goal.
    goals = tuple(g for g in goals if g and re.search(r"[0-9A-Za-z]", g))
    return goals


def _detect_initiation(text):
    """Return ('inline', goals_tuple) | ('ask',) | None for a Goals opener."""
    if not isinstance(text, str):
        return None
    m = _GOALS_INITIATION.match(text)
    if not m:
        return None
    rest = (m.group("rest") or "")
    rest = re.sub(r"^\s*(?:are|is|for\s+today|:|-|—|–)\s*", "", rest, flags=re.IGNORECASE)
    rest = rest.strip().strip(".;,! ")   # a trailing full stop is not a goal
    goals = _parse_goal_lines(rest)
    if goals:
        return ("inline", goals)
    return ("ask",)


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
    text = getattr(event, "text", None)
    literal = _parse_today_goals(text)                    # "Set today's goals:" (unchanged)
    initiation = _detect_initiation(text) if literal is None else None
    source = getattr(event, "source", None)

    # Nothing goals-shaped and no lane to check armed state against: fall through
    # cheaply, exactly as before (ordinary conversation is never intercepted).
    if literal is None and initiation is None and source is None:
        return None

    try:
        cfg = settings()
    except Exception:
        if literal is None and initiation is None:
            return None
        return "I couldn’t safely prepare today’s goals, so nothing was changed."
    if not is_governed_lane(source, {"gateway": {"signal_gate": cfg}}):
        return None

    armed = literal is None and initiation is None and goals_mode.is_armed(cfg, source)

    # Explicit opener with no goals stated: ask once and arm the one-shot window.
    if initiation is not None and initiation[0] == "ask":
        goals_mode.arm(cfg, event.source)
        return ("Sure — what are your top three goals for today? "
                "Send one to three, one per line.")

    goals = literal
    if goals is None and initiation is not None:          # ("inline", goals)
        goals = initiation[1]
    if goals is None and armed:
        # Christine has just asked for goals; this reply answers that question —
        # unless it is really an obligation / reminder / thought / Needs Bill,
        # which keeps its own routing (no goal is ever invented from it).
        goals_mode.disarm(cfg, event.source)
        if _looks_like_other_intent(text):
            return None
        goals = _parse_goal_lines(text)
    if goals is None:
        return None

    if not goals:
        return "Please send at least one goal, with one non-empty goal per line."
    if len(goals) > 3:
        return "Please choose your top three goals and resend them."
    goals_mode.disarm(cfg, event.source)                  # Goals is now underway
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
    return schema.render_human(proposal)


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
    display_text = schema.render_human(proposal)
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
            "Done. Today’s goals are updated.",
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
        await adapter.send(
            txn.chat_id,
            "I couldn’t update today’s goals. Nothing was changed.",
            reply_to=reply_to,
        )


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
    cfg = settings()
    if (
        not cfg.get("enabled")
        or str(getattr(event.source, "user_id", None)) != str(cfg.get("bill_user_id"))
        or str(getattr(event.source, "chat_id", None)) != str(cfg.get("chat_id"))
    ):
        return False

    kind = _classify_reply(getattr(event, "text", None))
    if kind == "other":
        # Not a confirmation reply — ordinary conversation always falls through.
        return False
    if getattr(event.source, "chat_type", None) != "dm":
        # Confirmations are lane-restricted; do not act, let it fall through.
        return False

    store = _store(cfg)
    # Recover any interrupted approval before evaluating what is pending.
    await reconcile(adapter)
    eligible = store.eligible(event.source.user_id, event.source.chat_id)
    if len(eligible) != 1:
        # No single pending goals proposal. A bare yes/no/maybe here approves
        # nothing, so fall through to ordinary conversation rather than hijack
        # it or claim a change.
        return False
    txn = eligible[0]

    if kind == "ambiguous":
        # Something is pending but the reply is unclear: briefly ask, commit
        # nothing, and do not consume the proposal.
        await adapter.send(
            event.source.chat_id,
            "Do you want me to replace today’s goals? Just say yes or no.",
            reply_to=event.message_id,
        )
        return True

    # Replay / duplicate protection: an already-consumed update never re-acts.
    if event.platform_update_id is None or store.update_seen(event.platform_update_id):
        await adapter.send(
            event.source.chat_id,
            "That reply was already handled. Nothing was changed.",
            reply_to=event.message_id,
        )
        return True

    if kind == "reject":
        store.consume_unbound(event.platform_update_id)
        store.supersede(txn.id)
        await adapter.send(
            event.source.chat_id,
            "Kept your current goals. Nothing was changed.",
            reply_to=event.message_id,
        )
        return True

    # kind == "affirm": run the existing durable claim + M3 application path.
    request_id = str(uuid.uuid4())
    request = _request(txn, event, request_id, store)
    if not store.claim(txn.id, event.platform_update_id, event.message_id, request_id, json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False), event.reply_to_message_id):
        await adapter.send(
            event.source.chat_id,
            "That reply was already handled. Nothing was changed.",
            reply_to=event.message_id,
        )
        return True
    await _apply(adapter, store, store.get(txn.id), request, event.message_id)
    return True

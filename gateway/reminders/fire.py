"""Due-time reminder fire path (runs as the cron ``no_agent`` launcher target).

Ordering at due time (all guarded, all idempotent):

1. look up the local execution record; none → nothing to fire, exit silent;
2. claim the deterministic delivery attempt — a replayed tick / restart that
   re-fires the one-shot before it is pruned loses the claim and exits silent;
3. ask canonical M3 (REMINDER_LIST) for the reminder's *current* status/version:
   cancelled, superseded (version moved) or already delivered → do not deliver;
   M3 unverifiable → release the claim and exit so reconcile can retry later;
4. deliver the current canonical wording through the existing Telegram path;
5. record delivered / failed / uncertain via REMINDER_DELIVERY_RECORD;
6. emit empty stdout so the scheduler itself sends nothing.

Collaborators are injectable (:class:`Deps`) so tests exercise every branch with
fakes — no real scheduler, no real Telegram send, no real canonical write.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from gateway.reminders import m3_client, schema

logger = logging.getLogger(__name__)

OUTCOME_DELIVERED = "delivered"
OUTCOME_FAILED = "failed"
OUTCOME_UNCERTAIN = "uncertain"


class _TelegramUncertain(Exception):
    """Ambiguous delivery result (network/timeout): the message may or may not
    have been sent, so the outcome is recorded as ``uncertain`` — never
    ``delivered``. Carries only a bounded, credential-free reason code."""


@dataclass
class Deps:
    store: object
    m3: object                       # module-like: call(), build_list(), build_delivery_record()
    deliver: Callable[[dict, str], tuple]   # (target, text) -> (ok: bool, error: Optional[str])


def attempt_id_for(reminder_id: str, version: int, due_rfc3339: Optional[str]) -> str:
    """Deterministic attempt id: the same due fire always yields the same id, so
    a replay dedups on M3 and cannot deliver twice locally."""
    return schema.canonical_hash({
        "reminder_id": reminder_id, "version": version, "due": due_rfc3339,
    })


def _now_rfc3339():
    from datetime import datetime
    from gateway.reminders.parse import SYDNEY
    return datetime.now(SYDNEY).isoformat()


async def _record(deps: Deps, reminder_id, base_version, attempt_id, outcome, error_code=None):
    # detail is the exact {attempted_at, error_code} the M3 candidate requires.
    request = deps.m3.build_delivery_record(
        request_id=str(uuid.uuid4()), reminder_id=reminder_id, base_version=base_version,
        attempt_id=attempt_id, outcome=outcome,
        detail={"attempted_at": _now_rfc3339(),
                "error_code": (error_code or None) if outcome != OUTCOME_DELIVERED else None},
    )
    return await deps.m3.call(request)


async def fire_async(reminder_id: str, deps: Deps) -> str:
    """Execute one due reminder. Returns a short machine status for logs/tests."""
    execution = deps.store.get_execution(reminder_id)
    if execution is None:
        return "no_execution_record"

    attempt_id = attempt_id_for(reminder_id, execution.version, execution.due_rfc3339)

    if not deps.store.claim_attempt(attempt_id, reminder_id, execution.version):
        state = deps.store.attempt_state(attempt_id)
        if state == "RECORDED":
            return "already_handled"
        # Claimed but never recorded (a prior crash mid-delivery): the outcome is
        # genuinely unknown — record uncertain once, idempotently, and stop.
        try:
            await _record(deps, reminder_id, execution.version, attempt_id,
                          OUTCOME_UNCERTAIN, error_code="crash_recovery")
            deps.store.record_attempt(attempt_id, OUTCOME_UNCERTAIN)
        except Exception as exc:
            logger.error("reminder %s: uncertain recovery record failed: %s", reminder_id, exc)
        return "recovered_uncertain"

    # Canonical guard — never deliver a stale/cancelled/superseded reminder.
    try:
        listing = await deps.m3.call(deps.m3.build_list(request_id=str(uuid.uuid4()), status="scheduled"))
    except Exception as exc:
        logger.error("reminder %s: M3 unverifiable at fire, deferring: %s", reminder_id, exc)
        deps.store.release_attempt(attempt_id)
        return "m3_unverifiable_deferred"

    current = _find(listing.get("reminders", []), reminder_id)
    if current is None or current.get("status") != "scheduled" or current.get("reminder_version") != execution.version:
        deps.store.record_attempt(attempt_id, "skipped")
        return "skipped_not_current"

    text = schema.clean_text(current.get("wording") or execution.text)
    target = json.loads(execution.target_json)

    try:
        # deliver may be sync (test fakes) or a coroutine (the live async send);
        # await it so the live path runs on the existing loop rather than nesting
        # asyncio.run() inside it.
        res = deps.deliver(target, text)
        if inspect.isawaitable(res):
            res = await res
        ok, error = res
    except _TelegramUncertain as exc:
        # Ambiguous send (network/timeout): record uncertain, never delivered.
        logger.warning("reminder %s: ambiguous delivery -> uncertain (%s)", reminder_id, exc)
        ok, error, outcome = False, str(exc)[:64], OUTCOME_UNCERTAIN
    except Exception:
        # Never log the raw exception text — a send-layer error can echo the bot
        # token inside its api.telegram.org/bot<TOKEN>/... URL.
        logger.error("reminder %s: delivery raised unexpectedly", reminder_id)
        ok, error, outcome = False, "delivery_exception", OUTCOME_UNCERTAIN
    else:
        outcome = OUTCOME_DELIVERED if ok else OUTCOME_FAILED

    try:
        await _record(deps, reminder_id, execution.version, attempt_id, outcome,
                      error_code=None if ok else (error or "")[:200])
        deps.store.record_attempt(attempt_id, outcome)
    except Exception as exc:
        # The message may have gone out, but we cannot confirm the canonical
        # record landed — that is honestly uncertain, not a claimed success.
        logger.error("reminder %s: delivery-record failed: %s", reminder_id, exc)
        deps.store.record_attempt(attempt_id, OUTCOME_UNCERTAIN)
        return "delivered_record_uncertain" if ok else "failed_record_uncertain"

    return f"recorded_{outcome}"


def _find(reminders, reminder_id):
    for item in reminders:
        if item.get("reminder_id") == reminder_id:
            return item
    return None


# --- default (live) wiring ---------------------------------------------------

def _bot_token() -> Optional[str]:
    """Load Christine's Telegram bot credential at fire time via the established
    Hermes loader ``hermes_cli.config.get_env_value`` (os.environ, then the
    durable ``~/.hermes/.env``).

    The cron ``no_agent`` subprocess env is passed through
    ``_sanitize_subprocess_env``, which strips ``TELEGRAM_BOT_TOKEN`` — so at
    fire time it is re-read here from the durable ``.env``. We do NOT widen the
    sanitizer; the credential stays in this short-lived process only and is never
    written to job metadata, the command line, jobs.json, M3, logs or stdout."""
    try:
        from hermes_cli.config import get_env_value
    except Exception:
        return None
    tok = get_env_value("TELEGRAM_BOT_TOKEN")
    tok = tok.strip() if isinstance(tok, str) else None
    return tok or None


def _classify_send_error(exc) -> str:
    """Map a send-layer exception to a bounded, credential-free reason code.

    A Telegram exception's own text can echo the bot token inside its
    api.telegram.org/bot<TOKEN>/... URL, so the raw text is NEVER propagated —
    only these fixed codes are returned/recorded/logged."""
    name = type(exc).__name__
    if any(k in name for k in ("TimedOut", "NetworkError", "RetryAfter", "Conflict")):
        return "network_or_timeout"
    if any(k in name for k in ("BadRequest", "Forbidden", "Unauthorized", "InvalidToken", "ChatMigrated")):
        return "telegram_rejected"
    return "send_error"


async def _default_deliver(target: dict, text: str) -> tuple:
    """Deliver a reminder through the existing Telegram send primitive.

    Awaited on the fire loop (never nests ``asyncio.run``). Returns
    ``(True, None)`` on delivery, or ``(False, <bounded code>)`` for a definite
    failure (recorded ``failed``). An ambiguous result (network/timeout) raises
    :class:`_TelegramUncertain` (recorded ``uncertain``). A missing credential
    returns ``(False, "missing_credential")`` and sends nothing. No credential
    value is ever returned, logged, raised or recorded."""
    token = _bot_token()
    if not token:
        # Honest, definite failure: cannot authenticate, so nothing is sent and
        # 'delivered' is never claimed.
        return False, "missing_credential"

    try:
        from gateway.config import Platform, load_gateway_config
        from tools.send_message_tool import _send_to_platform
    except Exception:
        return False, "send_import_error"

    platform_name = (target.get("platform") or "telegram").lower()
    try:
        platform = Platform(platform_name)
    except Exception:
        return False, "unknown_platform"

    pconfig = load_gateway_config().platforms.get(platform)
    if not pconfig:
        return False, "platform_not_configured"

    # Supply the credential to the existing send primitive ONLY within this
    # short-lived fire subprocess: the Telegram adapter reads
    # ``pconfig.token or os.getenv("TELEGRAM_BOT_TOKEN")`` at send time. Prefer
    # setting it on the freshly-loaded pconfig; fall back to a process-local env
    # var. Neither widens ``_sanitize_subprocess_env``.
    import os
    try:
        setattr(pconfig, "token", token)
    except Exception:
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    if not getattr(pconfig, "token", None):
        os.environ["TELEGRAM_BOT_TOKEN"] = token

    try:
        result = await _send_to_platform(platform, pconfig, str(target.get("chat_id")), text,
                                          thread_id=target.get("thread_id"))
    except Exception as exc:  # pragma: no cover - network path
        code = _classify_send_error(exc)
        if code == "network_or_timeout":
            raise _TelegramUncertain(code)   # ambiguous -> uncertain
        return False, code                    # definite rejection -> failed
    ok = getattr(result, "success", None)
    if ok is None:
        ok = bool(result)
    return (True, None) if ok else (False, "delivery_rejected")


def _default_deps() -> Deps:
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly
    from gateway.reminders.store import Store

    gateway = load_config_readonly().get("gateway", {}) if isinstance(load_config_readonly(), dict) else {}
    cfg = gateway.get("reminders_gate", {}) if isinstance(gateway, dict) else {}
    path = cfg.get("database") or str(get_hermes_home() / "reminders" / "state.sqlite")
    return Deps(store=Store(Path(path).expanduser()), m3=m3_client, deliver=_default_deliver)


def main(reminder_id: str, deps: Optional[Deps] = None) -> None:
    """Entry point invoked by the generated launcher. Never raises into the
    scheduler and never prints to stdout (the scheduler must send nothing)."""
    try:
        deps = deps or _default_deps()
        status = asyncio.run(fire_async(reminder_id, deps))
        logger.info("reminder %s fire: %s", reminder_id, status)
    except Exception as exc:  # pragma: no cover - last-resort guard
        logger.error("reminder %s fire crashed: %s", reminder_id, exc)
    # Intentionally emit nothing on stdout.

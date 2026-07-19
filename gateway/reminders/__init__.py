"""Reliable reminders (candidate).

An isolated Christine-plane feature that lets Bill set, view, change, snooze and
cancel reminders in natural language. Canonical ownership stays on M3 (reminder
definition, approval, version, status and recorded delivery outcome); Williams
owns only rebuildable execution — the scheduler clock, the Telegram delivery
attempt and local operational state.

Architecture (reuse-first — no new scheduler, no second canonical store):

* ``parse``            — natural-language date resolution via ``dateparser``,
                          pinned to ``Australia/Sydney`` and independent of the
                          Williams host timezone.
* ``schema``           — reminder payload validation + canonical hashing.
* ``m3_client``        — REMINDER_UPSERT / REMINDER_LIST / REMINDER_DELIVERY_RECORD
                          over the proven signal-gate SSH + receipt-correlation
                          pattern.
* ``store``            — local pending-confirmation state machine + a rebuildable
                          execution index (never the canonical wording/approval).
* ``scheduler_bridge`` — thin wrapper over the existing croniter cron *public*
                          surfaces (create/update/remove) + per-reminder launcher.
* ``fire``             — invoked by the launcher at due time: guard, deliver via
                          the existing Telegram path, record the outcome.
* ``service``          — orchestration: propose/confirm, view/change/snooze/cancel,
                          strict M3-first ordering, and restart reconciliation.

The whole feature is inert until ``gateway.reminders_gate.enabled`` is ``true``.
"""

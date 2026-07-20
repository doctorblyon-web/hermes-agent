"""Canonical PA objects (candidate): obligation and needs_bill.

An isolated Christine-plane feature that lets Bill record two kinds of personal
canonical object in natural language and move them through their explicit status
lifecycle:

* ``obligation``  — something Bill owes / is waiting on (open, waiting, completed,
                     cancelled).
* ``needs_bill``  — a decision only Bill can make (open, resolved, cancelled).

Canonical ownership stays on M3: the object definition, its preserved original
wording, every version and status transition live in the existing versioned PA
record substrate ``state/pa_v1_2/canonical.sqlite`` behind two bounded operations
(``PA_OBJECT_UPSERT`` / ``PA_OBJECT_LIST``), reusing the same idempotency ledger
and record-versioning as reminders. Williams owns only recognition, natural-date
resolution and the confirmation wording — no second canonical store, no scheduler.

Reuse-first (no new framework):

* ``schema``    — kinds, statuses, the exact transition tables, verbatim wording
                   preservation, canonical hashing and response verification.
* ``parse``     — conservative recognition of obligation / needs_bill creation and
                   their explicit transitions; Australia/Sydney *date* resolution
                   for an obligation due date (reuses the reminders date adapter).
* ``m3_client`` — PA_OBJECT_UPSERT / PA_OBJECT_LIST over the same signal-gate SSH
                   round-trip + response-seal verification as reminders.
* ``store``     — a local Telegram-update idempotency ledger so a replayed update
                   never creates or transitions a second object.
* ``service``   — orchestration: recognise, resolve the target, apply M3-first,
                   confirm in normal language; ordinary conversation falls through.

The whole feature is inert until ``gateway.pa_objects_gate.enabled`` is ``true``.
There is deliberately no delete path anywhere: objects only reach a terminal
status, never removal.
"""

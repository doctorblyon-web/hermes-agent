"""Bridge to the existing croniter cron scheduler — public surfaces only.

Every scheduler interaction goes through cron's public functions
(``create_job`` / ``update_job`` / ``remove_job`` / ``list_jobs``); no scheduler
internals are touched. A reminder becomes exactly one ``no_agent`` one-shot cron
job whose ``script`` is a tiny generated launcher. At due time the scheduler runs
the launcher, which hands off to :mod:`gateway.reminders.fire` — the fire step
guards, delivers via the existing Telegram path and records the outcome, then
emits empty stdout so the scheduler itself sends nothing.

The cron module is injected (defaulting to the real ``cron.jobs``) so unit tests
drive a fake and never create real jobs, spawn the scheduler, or write to
``~/.hermes/cron/jobs.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

# Marker stored in the cron job's origin so reconcile/list can recognise a
# reminder-owned job without inspecting anything canonical.
ORIGIN_KIND = "billos_reminder"

_LAUNCHER_TEMPLATE = '''\
#!/usr/bin/env python3
# AUTO-GENERATED reminder launcher — do not edit.
# Reuses the existing cron no_agent script path: the scheduler runs this at the
# reminder's due time; it delegates to gateway.reminders.fire which guards
# against a cancelled/superseded/already-delivered reminder, delivers via the
# existing Telegram path, and records the delivery outcome on M3.
import sys
sys.path.insert(0, {repo_root!r})
from gateway.reminders import fire
fire.main({reminder_id!r})
'''


def _cron():
    import cron.jobs as jobs
    return jobs


def _repo_root() -> str:
    return str(Path(__file__).resolve().parent.parent.parent)


def _scripts_dir() -> Path:
    return get_hermes_home() / "scripts"


def _launcher_path(reminder_id: str) -> Path:
    return _scripts_dir() / f"reminder_{reminder_id}.py"


def _write_launcher(reminder_id: str) -> str:
    """Generate the per-reminder launcher script and return its path."""
    scripts = _scripts_dir()
    scripts.mkdir(parents=True, exist_ok=True)
    path = _launcher_path(reminder_id)
    path.write_text(_LAUNCHER_TEMPLATE.format(repo_root=_repo_root(), reminder_id=reminder_id))
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return str(path)


def _origin(reminder_id: str, version: int, target: dict) -> dict:
    """Rebuildable execution metadata carried on the cron job — never wording
    or approval authority, only what is needed to link the job to canonical M3."""
    return {
        "kind": ORIGIN_KIND,
        "reminder_id": reminder_id,
        "reminder_version": version,
        "platform": target.get("platform"),
        "chat_id": target.get("chat_id"),
    }


def create_oneshot(*, reminder_id: str, version: int, due_rfc3339: str,
                   target: dict, cron=None) -> str:
    """Create exactly one local one-shot job for a scheduled reminder.

    Returns the cron job id. The job runs the launcher (``no_agent``) and
    delivers ``local`` (silent) — fire.py owns the actual Telegram send, so the
    scheduler never double-delivers.
    """
    jobs = cron or _cron()
    script_path = _write_launcher(reminder_id)
    job = jobs.create_job(
        prompt=None,
        schedule=due_rfc3339,
        name=f"Reminder {reminder_id}",
        repeat=1,
        deliver="local",
        origin=_origin(reminder_id, version, target),
        script=script_path,
        no_agent=True,
    )
    return job["id"]


def remove_oneshot(cron_job_id: str, reminder_id: Optional[str] = None, cron=None) -> bool:
    """Remove the local one-shot job and its launcher script."""
    jobs = cron or _cron()
    removed = False
    if cron_job_id:
        removed = bool(jobs.remove_job(cron_job_id))
    if reminder_id:
        try:
            _launcher_path(reminder_id).unlink(missing_ok=True)
        except OSError:
            pass
    return removed


def replace_oneshot(*, reminder_id: str, version: int, due_rfc3339: str, target: dict,
                    old_cron_job_id: Optional[str], cron=None) -> str:
    """Replace a reminder's local job (change / snooze): remove then recreate.

    Recreating rather than mutating in place keeps the launcher, origin version
    and next_run_at consistently derived from the new canonical state.
    """
    jobs = cron or _cron()
    if old_cron_job_id:
        try:
            jobs.remove_job(old_cron_job_id)
        except Exception:
            pass
    return create_oneshot(reminder_id=reminder_id, version=version,
                          due_rfc3339=due_rfc3339, target=target, cron=jobs)


def get_job(job_id: str, cron=None):
    jobs = cron or _cron()
    return jobs.get_job(job_id)


def verify_linked(job_id: str, reminder_id: str, cron=None) -> bool:
    """Confirm a just-created job exists and is linked to the canonical reminder.

    Used before claiming success: a reminder is only "set" once its local job
    provably exists and carries the matching canonical id in its origin.
    """
    job = get_job(job_id, cron=cron)
    if not job:
        return False
    origin = job.get("origin")
    return (
        isinstance(origin, dict)
        and origin.get("kind") == ORIGIN_KIND
        and origin.get("reminder_id") == reminder_id
    )


def list_reminder_jobs(cron=None) -> list:
    """All cron jobs owned by the reminder feature (by origin marker)."""
    jobs = cron or _cron()
    out = []
    for job in jobs.list_jobs(include_disabled=True):
        origin = job.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == ORIGIN_KIND:
            out.append(job)
    return out


def target_json(target: dict) -> str:
    return json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

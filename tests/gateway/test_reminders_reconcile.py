"""Restart reconciliation: make local scheduler jobs match canonical M3."""

import asyncio

from gateway.reminders import service
from gateway.reminders.store import Store
from tests.gateway.reminder_helpers import FakeBridge, FakeCron, FakeM3, cfg

run = asyncio.run
RID = "rem_" + "a" * 24
DUE_LOCAL = "2026-07-20T09:00:00+10:00"


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "settings", lambda: cfg(tmp_path))
    return Store(cfg(tmp_path)["database"]), FakeM3(), FakeBridge(), FakeCron()


def _exec(store, *, version=1, cron_job_id=None, reminder_id=RID):
    store.upsert_execution(reminder_id=reminder_id, version=version, status="scheduled",
                           text="review SignalPath", due_rfc3339=DUE_LOCAL,
                           timezone="Australia/Sydney", target_json='{"platform":"telegram","chat_id":"9"}',
                           cron_job_id=cron_job_id)


def test_rebuilds_missing_job_from_canonical(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    m3.seed(RID, version=1, wording="review SignalPath")
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert RID in bridge.jobs                            # job recreated
    ex = store.get_execution(RID)
    assert ex is not None and ex.cron_job_id == bridge.jobs[RID] and ex.version == 1


def test_does_not_duplicate_a_correct_existing_job(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    m3.seed(RID, version=1, wording="review SignalPath")
    _exec(store, version=1, cron_job_id="job1")
    bridge.jobs[RID] = "job1"                            # already correctly linked
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert bridge.jobs == {RID: "job1"}                 # untouched
    assert bridge._n == 0                               # no new job created


def test_version_change_replaces_job(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    m3.seed(RID, version=2, wording="review SignalPath")
    _exec(store, version=1, cron_job_id="jobOld")
    bridge.jobs[RID] = "jobOld"
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert store.get_execution(RID).version == 2
    assert bridge.jobs[RID] != "jobOld"                 # replaced, still exactly one
    assert list(bridge.jobs) == [RID]


def test_removes_job_for_no_longer_scheduled_reminder(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    # Canonical has nothing scheduled; local still holds a stale job.
    _exec(store, version=1, cron_job_id="job2", reminder_id=RID)
    bridge.jobs[RID] = "job2"
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert store.get_execution(RID) is None
    assert bridge.jobs == {}


def test_m3_unreachable_makes_no_destructive_change(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    _exec(store, version=1, cron_job_id="job1")
    bridge.jobs[RID] = "job1"
    m3.fail = True
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert store.get_execution(RID).cron_job_id == "job1"
    assert bridge.jobs == {RID: "job1"}


def test_inflight_confirmation_recovered_to_indeterminate(monkeypatch, tmp_path):
    store, m3, bridge, cron = _wire(monkeypatch, tmp_path)
    pid = store.prepare(action="create", bill_user_id="1", chat_id="9", reminder_id=None,
                        base_version=None, source_update_id="1", proposal_sha256="x",
                        proposal_json="{}")
    store.arm(pid, "")
    assert store.claim(pid, "50", "50", "req1", "{}") == "req1"   # -> PROCESSING
    run(service.reconcile(m3=m3, bridge=bridge, cron=cron, all_inflight=True))
    assert store.get(pid).state == "INDETERMINATE"

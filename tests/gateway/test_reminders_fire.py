"""Due-time fire path: guard, deliver once, record outcome — all idempotent."""

import asyncio
import json

from gateway.reminders import fire
from gateway.reminders.store import Store
from tests.gateway.reminder_helpers import FakeDeliver, FakeM3

run = asyncio.run
TARGET = {"platform": "telegram", "chat_id": "9"}
DUE_LOCAL = "2026-07-20T09:00:00+10:00"


def _setup(tmp_path, *, version=1, status="scheduled", m3_version=None):
    store = Store(tmp_path / "r.sqlite")
    store.upsert_execution(
        reminder_id="rem_" + "0" * 24, version=version, status="scheduled", text="check Christine",
        due_rfc3339=DUE_LOCAL, timezone="Australia/Sydney",
        target_json=json.dumps(TARGET, sort_keys=True, separators=(",", ":")),
    )
    m3 = FakeM3()
    m3.seed("rem_" + "0" * 24, version=m3_version or version, status=status, wording="check Christine")
    deliver = FakeDeliver()
    return store, m3, deliver, fire.Deps(store=store, m3=m3, deliver=deliver)


RID = "rem_" + "0" * 24


def test_delivers_once_and_records_delivered(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path)
    status = run(fire.fire_async(RID, deps))
    assert status == "recorded_delivered"
    assert deliver.sent == [(TARGET, "check Christine")]
    # exactly one delivery recorded on M3, outcome delivered
    assert m3.recorded == ["delivered"]


def test_duplicate_tick_does_not_deliver_twice(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path)
    run(fire.fire_async(RID, deps))
    status2 = run(fire.fire_async(RID, deps))     # replayed tick / restart re-fire
    assert status2 == "already_handled"
    assert len(deliver.sent) == 1                   # only ever one send
    assert m3.recorded == ["delivered"]


def test_cancelled_reminder_is_not_delivered(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path, status="cancelled")
    status = run(fire.fire_async(RID, deps))
    assert status == "skipped_not_current"
    assert deliver.sent == []
    assert m3.recorded == []


def test_superseded_version_is_not_delivered(tmp_path):
    # Local job is for v1 but canonical has moved to v2 (changed/snoozed since).
    store, m3, deliver, deps = _setup(tmp_path, version=1, m3_version=2)
    status = run(fire.fire_async(RID, deps))
    assert status == "skipped_not_current"
    assert deliver.sent == []


def test_m3_unverifiable_defers_and_releases_claim(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path)
    m3.fail = True
    status = run(fire.fire_async(RID, deps))
    assert status == "m3_unverifiable_deferred"
    assert deliver.sent == []
    # Claim was released, so once M3 recovers the reminder still fires.
    m3.fail = False
    status2 = run(fire.fire_async(RID, deps))
    assert status2 == "recorded_delivered"
    assert deliver.sent == [(TARGET, "check Christine")]


def test_delivery_failure_records_failed(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path)
    deps.deliver = FakeDeliver(ok=False, error="telegram 502")
    status = run(fire.fire_async(RID, deps))
    assert status == "recorded_failed"
    assert m3.recorded == ["failed"]


def test_delivery_exception_records_uncertain(tmp_path):
    store, m3, deliver, deps = _setup(tmp_path)
    deps.deliver = FakeDeliver(raises=RuntimeError("boom"))
    status = run(fire.fire_async(RID, deps))
    assert status == "recorded_uncertain"
    assert m3.recorded == ["uncertain"]


def test_no_execution_record_is_silent(tmp_path):
    store = Store(tmp_path / "r.sqlite")
    m3 = FakeM3()
    deps = fire.Deps(store=store, m3=m3, deliver=FakeDeliver())
    assert run(fire.fire_async("MISSING", deps)) == "no_execution_record"

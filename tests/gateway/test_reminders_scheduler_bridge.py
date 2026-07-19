"""The real scheduler bridge: one-shot creation, launcher, linkage, replace/remove.

Uses the real ``scheduler_bridge`` against a fake cron and an isolated
``HERMES_HOME`` (a tmp dir) so launcher scripts never touch the live ~/.hermes
and no real scheduler job is created.
"""

import pytest

from gateway.reminders import scheduler_bridge as bridge
from tests.gateway.reminder_helpers import FakeCron

DUE = "2026-07-20T09:00:00+10:00"
TARGET = {"platform": "telegram", "chat_id": "9"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_create_oneshot_makes_one_linked_no_agent_job(home):
    cron = FakeCron()
    job_id = bridge.create_oneshot(reminder_id="R1", version=1, due_rfc3339=DUE,
                                   target=TARGET, cron=cron)
    job = cron.get_job(job_id)
    assert job["no_agent"] is True
    assert job["schedule"]["run_at"] == DUE
    assert job["repeat"]["times"] == 1                      # one-shot
    assert job["deliver"] == "local"                        # fire.py self-delivers
    # origin carries only rebuildable execution metadata (id/version/target) —
    # never the wording or approval authority.
    origin = job["origin"]
    assert origin["kind"] == bridge.ORIGIN_KIND
    assert origin["reminder_id"] == "R1" and origin["reminder_version"] == 1
    assert "text" not in origin and "approved_by" not in origin
    # launcher script exists under the isolated HERMES_HOME/scripts and delegates.
    launcher = home / "scripts" / "reminder_R1.py"
    assert launcher.exists()
    body = launcher.read_text()
    assert "gateway.reminders" in body and "fire.main('R1')" in body
    assert bridge.verify_linked(job_id, "R1", cron=cron)


def test_replace_oneshot_swaps_job_and_keeps_one(home):
    cron = FakeCron()
    first = bridge.create_oneshot(reminder_id="R1", version=1, due_rfc3339=DUE,
                                  target=TARGET, cron=cron)
    second = bridge.replace_oneshot(reminder_id="R1", version=2, due_rfc3339=DUE,
                                    target=TARGET, old_cron_job_id=first, cron=cron)
    assert first != second
    assert cron.get_job(first) is None                      # old removed
    assert cron.get_job(second)["origin"]["reminder_version"] == 2
    assert len(bridge.list_reminder_jobs(cron=cron)) == 1


def test_remove_oneshot_deletes_job_and_launcher(home):
    cron = FakeCron()
    job_id = bridge.create_oneshot(reminder_id="R1", version=1, due_rfc3339=DUE,
                                   target=TARGET, cron=cron)
    launcher = home / "scripts" / "reminder_R1.py"
    assert launcher.exists()
    assert bridge.remove_oneshot(job_id, "R1", cron=cron) is True
    assert cron.get_job(job_id) is None
    assert not launcher.exists()


def test_verify_linked_rejects_wrong_or_missing_job(home):
    cron = FakeCron()
    job_id = bridge.create_oneshot(reminder_id="R1", version=1, due_rfc3339=DUE,
                                   target=TARGET, cron=cron)
    assert bridge.verify_linked(job_id, "R1", cron=cron) is True
    assert bridge.verify_linked(job_id, "OTHER", cron=cron) is False
    assert bridge.verify_linked("nope", "R1", cron=cron) is False


def test_list_reminder_jobs_filters_by_origin(home):
    cron = FakeCron()
    bridge.create_oneshot(reminder_id="R1", version=1, due_rfc3339=DUE, target=TARGET, cron=cron)
    cron.create_job(schedule="every 5m", name="unrelated", origin={"kind": "other"})
    jobs = bridge.list_reminder_jobs(cron=cron)
    assert len(jobs) == 1 and jobs[0]["origin"]["reminder_id"] == "R1"

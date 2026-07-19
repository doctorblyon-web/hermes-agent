"""Due-time fire path: guard, deliver once, record outcome — all idempotent."""

import asyncio
import json
import os
import types

import pytest

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


# --- credential loading & _default_deliver (the delivery correction) ---------

# Bounded, credential-free reason codes the delivery path may emit. No code here
# contains or could contain the bot token.
_BOUNDED_CODES = {
    "missing_credential", "send_import_error", "unknown_platform",
    "platform_not_configured", "delivery_rejected", "telegram_rejected",
    "send_error", "network_or_timeout",
}


def test_sanitized_subprocess_env_strips_telegram_token():
    # The cron no_agent subprocess (where fire runs) never receives the token.
    from tools.environments.local import _sanitize_subprocess_env
    san = _sanitize_subprocess_env({**os.environ, "TELEGRAM_BOT_TOKEN": "SECRET"})
    assert "TELEGRAM_BOT_TOKEN" not in san


def test_bot_token_loads_from_process_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-ENV-123")
    assert fire._bot_token() == "SECRET-ENV-123"


def test_bot_token_loads_from_durable_env_file(monkeypatch, tmp_path):
    from hermes_cli.config import invalidate_env_cache
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=SECRET-FILE-456\n")
    invalidate_env_cache()
    try:
        assert fire._bot_token() == "SECRET-FILE-456"
    finally:
        invalidate_env_cache()


def test_missing_credential_does_not_send_and_fails_honestly(monkeypatch, tmp_path):
    from hermes_cli.config import invalidate_env_cache
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))   # tmp home has no .env
    invalidate_env_cache()
    import tools.send_message_tool as smt
    called = []
    monkeypatch.setattr(smt, "_send_to_platform", lambda *a, **k: called.append(1))
    try:
        ok, code = run(fire._default_deliver(TARGET, "check Christine"))
    finally:
        invalidate_env_cache()
    assert ok is False and code == "missing_credential"
    assert called == []                       # never attempted a send
    assert "SECRET" not in code and code in _BOUNDED_CODES


def _patch_send(monkeypatch, send):
    import gateway.config as gc
    import tools.send_message_tool as smt

    class _PC:
        token = None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN-XYZ")
    monkeypatch.setattr(gc, "load_gateway_config",
                        lambda: types.SimpleNamespace(platforms={gc.Platform.TELEGRAM: _PC()}))
    monkeypatch.setattr(smt, "_send_to_platform", send)


def test_successful_send_reports_delivered(monkeypatch):
    async def ok_send(platform, pconfig, chat, text, thread_id=None):
        assert getattr(pconfig, "token", None) == "SECRET-TOKEN-XYZ"   # credential wired in
        return types.SimpleNamespace(success=True)
    _patch_send(monkeypatch, ok_send)
    ok, code = run(fire._default_deliver(TARGET, "check Christine"))
    assert ok is True and code is None


def test_definite_rejection_reports_failed(monkeypatch):
    class BadRequest(Exception):
        pass
    async def bad_send(*a, **k):
        # A real telegram error can echo the token in its URL; we must not leak it.
        raise BadRequest("Chat not found https://api.telegram.org/botSECRET-TOKEN-XYZ/sendMessage")
    _patch_send(monkeypatch, bad_send)
    ok, code = run(fire._default_deliver(TARGET, "check Christine"))
    assert ok is False and code == "telegram_rejected"
    assert "SECRET-TOKEN-XYZ" not in code and code in _BOUNDED_CODES


def test_timeout_raises_uncertain(monkeypatch):
    class TimedOut(Exception):
        pass
    async def slow_send(*a, **k):
        raise TimedOut("Timed out")
    _patch_send(monkeypatch, slow_send)
    with pytest.raises(fire._TelegramUncertain) as ei:
        run(fire._default_deliver(TARGET, "check Christine"))
    assert str(ei.value) == "network_or_timeout"


def test_classify_send_error_is_bounded():
    class TimedOut(Exception): pass
    class BadRequest(Exception): pass
    class Whatever(Exception): pass
    assert fire._classify_send_error(TimedOut()) == "network_or_timeout"
    assert fire._classify_send_error(BadRequest()) == "telegram_rejected"
    assert fire._classify_send_error(Whatever()) == "send_error"


def test_end_to_end_default_deliver_delivered_records_delivered(monkeypatch, tmp_path):
    # fire_async driving the REAL _default_deliver with a mocked send -> delivered.
    async def ok_send(platform, pconfig, chat, text, thread_id=None):
        return types.SimpleNamespace(success=True)
    _patch_send(monkeypatch, ok_send)
    store, m3, _dev, _deps = _setup(tmp_path)
    deps = fire.Deps(store=store, m3=m3, deliver=fire._default_deliver)
    status = run(fire.fire_async(RID, deps))
    assert status == "recorded_delivered"
    assert m3.recorded == ["delivered"]

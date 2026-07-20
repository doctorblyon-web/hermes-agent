"""The model-callable ``pa_object`` tool: registration and dispatch wiring.

These tests exercise the thin tool layer that exposes the bounded PA-object
write to Christine's model. The write pipeline itself (M3-first, idempotent,
no-delete, verbatim wording) is covered by test_pa_objects_service.py; here we
prove the tool is registered, gated, and that a model tool call is routed to the
right service function with exactly the structured arguments the model supplied
(and the session id needed for idempotency).
"""

import json

import pytest

import tools.pa_object_tool as pa_tool  # registers the tool on import
from gateway.pa_objects import service
from tools.registry import registry


def test_tool_is_registered_async_and_gated():
    entry = registry.get_entry("pa_object")
    assert entry is not None
    assert entry.toolset == "pa_object"
    assert entry.is_async is True
    assert callable(entry.check_fn)


def test_check_fn_follows_gate(monkeypatch):
    monkeypatch.setattr(service, "settings", lambda: {"enabled": True})
    assert pa_tool._gate_enabled() is True
    monkeypatch.setattr(service, "settings", lambda: {"enabled": False})
    assert pa_tool._gate_enabled() is False
    def boom():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(service, "settings", boom)
    assert pa_tool._gate_enabled() is False   # never crashes the schema build


def _capture(monkeypatch):
    calls = {}
    async def fake_create(kind, wording, *, person=None, due=None, project=None, session_id=None, **_):
        calls["create"] = dict(kind=kind, wording=wording, person=person, due=due,
                               project=project, session_id=session_id)
        return {"ok": True, "object_id": "obl_1", "kind": kind, "status": "open",
                "object_version": 1, "message": "Recorded."}
    async def fake_transition(kind, action, *, reference=None, object_id=None,
                              decision=None, session_id=None, **_):
        calls["transition"] = dict(kind=kind, action=action, reference=reference,
                                   object_id=object_id, decision=decision, session_id=session_id)
        return {"ok": True, "object_id": "obl_1", "kind": kind, "status": "completed",
                "object_version": 2, "message": "Done."}
    monkeypatch.setattr(service, "tool_create", fake_create)
    monkeypatch.setattr(service, "tool_transition", fake_transition)
    return calls


def test_dispatch_create_passes_structured_args_and_session(monkeypatch):
    calls = _capture(monkeypatch)
    out = registry.dispatch("pa_object", {
        "operation": "create", "kind": "obligation",
        "wording": "send Gina the revised material by Friday",
        "person": "Gina", "due": "Friday",
    }, session_id="sess-abc")
    result = json.loads(out)
    assert result["ok"] is True and result["object_id"] == "obl_1"
    assert calls["create"] == {
        "kind": "obligation", "wording": "send Gina the revised material by Friday",
        "person": "Gina", "due": "Friday", "project": None, "session_id": "sess-abc",
    }


def test_dispatch_transition_passes_reference_and_decision(monkeypatch):
    calls = _capture(monkeypatch)
    out = registry.dispatch("pa_object", {
        "operation": "transition", "kind": "needs_bill", "action": "resolve",
        "reference": "pilot", "decision": "do not start it yet",
    }, session_id="sess-xyz")
    result = json.loads(out)
    assert result["ok"] is True
    assert calls["transition"] == {
        "kind": "needs_bill", "action": "resolve", "reference": "pilot",
        "object_id": None, "decision": "do not start it yet", "session_id": "sess-xyz",
    }


def test_dispatch_create_missing_wording_refuses_without_calling_service(monkeypatch):
    calls = _capture(monkeypatch)
    out = registry.dispatch("pa_object", {"operation": "create", "kind": "obligation"},
                            session_id="s")
    result = json.loads(out)
    assert result["ok"] is False
    assert "create" not in calls   # never reached the canonical write


def test_dispatch_transition_needs_a_target(monkeypatch):
    calls = _capture(monkeypatch)
    out = registry.dispatch("pa_object", {
        "operation": "transition", "kind": "obligation", "action": "complete",
    }, session_id="s")
    result = json.loads(out)
    assert result["ok"] is False
    assert "transition" not in calls


def test_dispatch_unknown_operation_refuses(monkeypatch):
    _capture(monkeypatch)
    out = registry.dispatch("pa_object", {"operation": "delete", "kind": "obligation"},
                            session_id="s")
    assert json.loads(out)["ok"] is False

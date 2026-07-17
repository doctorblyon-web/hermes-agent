"""Focused tests for THOUGHTS_NOT_TO_LOSE_01.

Explicit natural thought capture on the existing Raw Capture path, using M3's
additive typed capture shape (object_type="thought").

The M3 transport is stubbed, but every stubbed response is passed through the
real ``m3_client.verify_response`` so verification is genuinely exercised.
"""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from gateway.raw_capture_gate import m3_client
from gateway.raw_capture_gate import service as raw_capture
from gateway.raw_capture_gate import schema as capture_schema
from gateway.thoughts_gate import schema as thoughts_schema
from gateway.thoughts_gate import service as thoughts

BILL = "8051024863"
EVENT_ID = "RAW_CAPTURE_APPEND-20260717T090000000000Z-bill"
CAPTURE_ID = "cap_" + "a" * 32
CANONICAL_THOUGHT = "the dashboard should show attention, not activity."


class Src:
    platform = "telegram"
    chat_type = "dm"

    def __init__(self, user_id=BILL, chat_id=BILL):
        self.user_id = user_id
        self.chat_id = chat_id


class Ev:
    raw_message = None
    media_urls = ()
    media_types = ()

    def __init__(self, text, update_id="u1", message_id="m1", source=None):
        self.text = text
        self.source = source or Src()
        self.platform_update_id = update_id
        self.message_id = message_id
        self.timestamp = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


def _applied_response(request, *, object_type=None, receipt_type=None, omit_type=False):
    """Build a well-formed APPLIED response and sign it like M3 does."""
    original = request["capture"]["original_text"]
    content = hashlib.sha256(original.encode("utf-8")).hexdigest()
    receipt = {
        "receipt_id": f"{EVENT_ID}.20260717T090000Z.txt",
        "receipt_event_id": EVENT_ID,
        "receipt_sha256": "b" * 64,
        "operation": "RAW_CAPTURE_APPEND",
        "request_id": request["request_id"],
        "capture_id": CAPTURE_ID,
        "content_sha256": content,
        "canonical_state_sha256": "c" * 64,
        "applied_by": "process-event-v0.3",
        "processed_at": "2026-07-17T09:00:00Z",
        "updated_by": "bill",
        "receipt_recovered": False,
    }
    response = {
        "version": 1,
        "request_id": request["request_id"],
        "status": "APPLIED",
        "capture_id": CAPTURE_ID,
        "content_sha256": content,
        "event_id": EVENT_ID,
        "operation": "RAW_CAPTURE_APPEND",
        "event_created_at": "2026-07-17T09:00:00Z",
        "applied_at": "2026-07-17T09:00:00Z",
        "process_receipt": receipt,
    }
    sent_type = request["capture"].get("object_type")
    if sent_type is not None and not omit_type:
        response["object_type"] = object_type or sent_type
        receipt["object_type"] = receipt_type or sent_type
    response["response_sha256"] = m3_client.canonical_hash(response)
    return response


def _failure_response(request, status, code):
    response = {
        "version": 1,
        "request_id": request["request_id"],
        "status": status,
        "applied": False if status == "REJECTED" else None,
        "error": {"code": code, "message": "m3 said no"},
    }
    response["response_sha256"] = m3_client.canonical_hash(response)
    return response


@pytest.fixture
def m3(tmp_path, monkeypatch):
    """Stub the transport; keep real verification and real durable state."""

    class Fake:
        def __init__(self):
            self.requests = []
            self.builder = _applied_response

        async def call_serialized(self, request_json, *, timeout=12):
            self.requests.append(json.loads(request_json))
            request = json.loads(request_json)
            response = self.builder(request)
            return m3_client.verify_response(response, request)

        @property
        def sent(self):
            return self.requests[-1]

    fake = Fake()
    monkeypatch.setattr(
        raw_capture, "settings",
        lambda: {
            "enabled": True, "bill_user_id": BILL, "chat_id": BILL,
            "database": str(tmp_path / "transactions.sqlite"),
        },
    )
    monkeypatch.setattr(m3_client, "call_serialized", fake.call_serialized)
    return fake


# --- explicit intent detection -------------------------------------------

ACCEPTED = [
    "Don’t let me lose this thought — the dashboard should show attention, not activity.",
    "Don't let me lose this thought - the dashboard should show attention, not activity.",
    "Save this as a thought: the dashboard should show attention, not activity.",
    "Remember this idea as a thought: the dashboard should show attention, not activity.",
    "Capture this thought: the dashboard should show attention, not activity.",
    "capture this thought: the dashboard should show attention, not activity.",
    "  Save this as a thought:   the dashboard should show attention, not activity.  ",
]

ORDINARY = [
    "the dashboard should show attention, not activity",
    "I think the dashboard should show attention, not activity.",
    "Here's an idea: the dashboard should show attention, not activity.",
    "I had a thought about the dashboard.",
    "What do you think about the dashboard?",
    "Remember to call the dentist tomorrow.",
    "Save this file for me.",
    "hello",
    "",
]


@pytest.mark.parametrize("text", ACCEPTED)
def test_each_explicit_phrase_extracts_exact_thought(text):
    assert thoughts_schema.parse(text) == CANONICAL_THOUGHT


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_conversation_is_not_a_thought_request(text):
    assert thoughts_schema.parse(text) is None
    assert thoughts_schema.is_thought_request(text) is False


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_conversation_is_not_intercepted(m3, text):
    assert asyncio_run(thoughts.intercept(Ev(text))) is None
    assert m3.requests == []


# --- exact wording preservation ------------------------------------------

def test_canonical_example_saves_and_stores_exact_text(m3):
    event = Ev(
        "Don’t let me lose this thought — the dashboard should show attention, not activity."
    )
    assert asyncio_run(thoughts.intercept(event)) == "Saved as a thought."
    assert m3.sent["capture"]["original_text"] == CANONICAL_THOUGHT


def test_no_thought_prefix_is_stored(m3):
    asyncio_run(thoughts.intercept(Ev("Save this as a thought: ship it.")))
    stored = m3.sent["capture"]["original_text"]
    assert stored == "ship it."
    assert not stored.lower().startswith("thought")


@pytest.mark.parametrize(
    "payload",
    [
        "café — naïve — 日本語 — emoji 🧠",
        "line one\nline two\n\nline four",
        "trailing internal  double  spaces kept",
        "Ünicode: ß, ø, é and a trailing dot.",
    ],
)
def test_unicode_and_newlines_preserved_byte_for_byte(m3, payload):
    asyncio_run(thoughts.intercept(Ev(f"Capture this thought: {payload}")))
    assert m3.sent["capture"]["original_text"] == payload


def test_surrounding_whitespace_is_stripped_but_inner_text_untouched(m3):
    asyncio_run(thoughts.intercept(Ev("Save this as a thought:   spaced out   ")))
    assert m3.sent["capture"]["original_text"] == "spaced out"


def test_content_hash_binds_the_exact_stored_text(m3):
    asyncio_run(thoughts.intercept(Ev("Capture this thought: exactly this")))
    capture = m3.sent["capture"]
    assert capture["content_sha256"] == hashlib.sha256(b"exactly this").hexdigest()


def test_instruction_without_a_thought_saves_nothing(m3):
    assert asyncio_run(thoughts.intercept(Ev("Save this as a thought:   "))) == (
        "I didn’t catch the thought itself, so nothing was saved."
    )
    assert m3.requests == []


# --- typed shape ----------------------------------------------------------

def test_object_type_thought_is_sent(m3):
    asyncio_run(thoughts.intercept(Ev("Capture this thought: typed please")))
    assert m3.sent["capture"]["object_type"] == "thought"
    assert m3.sent["operation"] == "RAW_CAPTURE_APPEND"
    assert set(m3.sent["capture"]) == {
        "original_text", "content_sha256", "triage_state", "triage_reason",
        "object_type",
    }


def test_typed_response_missing_object_type_is_rejected(m3):
    m3.builder = lambda request: _applied_response(request, omit_type=True)
    assert asyncio_run(thoughts.intercept(Ev("Capture this thought: x"))) != (
        "Saved as a thought."
    )


def test_typed_response_with_wrong_object_type_is_rejected(m3):
    m3.builder = lambda request: _applied_response(request, object_type="task")
    assert asyncio_run(thoughts.intercept(Ev("Capture this thought: x"))) != (
        "Saved as a thought."
    )


def test_typed_receipt_with_wrong_object_type_is_rejected(m3):
    m3.builder = lambda request: _applied_response(request, receipt_type="task")
    assert asyncio_run(thoughts.intercept(Ev("Capture this thought: x"))) != (
        "Saved as a thought."
    )


def test_unsupported_object_type_never_reaches_m3(m3):
    assert asyncio_run(
        raw_capture.capture_result(Ev("x"), "x", object_type="goal")
    ) is None
    assert m3.requests == []


# --- legacy compatibility -------------------------------------------------

def test_legacy_request_bytes_are_unchanged():
    """The exact legacy wire bytes, asserted literally."""
    parsed = capture_schema.validate_text("plain capture")
    request = raw_capture._build_request("rid-1", Ev("plain capture"), parsed, "2026-07-17T09:00:00Z")
    assert request["capture"] == {
        "original_text": "plain capture",
        "content_sha256": hashlib.sha256(b"plain capture").hexdigest(),
        "triage_state": "untriaged",
        "triage_reason": None,
    }
    assert "object_type" not in request["capture"]
    assert "object_type" not in json.dumps(request)


def test_legacy_replay_identity_is_unchanged():
    parsed = capture_schema.validate_text("plain capture")
    identity = raw_capture._request_hash(Ev("plain capture"), parsed, "2026-07-17T09:00:00Z")
    expected = hashlib.sha256(json.dumps({
        "source": {
            "platform": "telegram", "user_id": BILL, "chat_id": BILL,
            "update_id": "u1", "message_id": "m1",
            "captured_at": "2026-07-17T09:00:00Z",
        },
        "capture": {
            "original_text": "plain capture",
            "content_sha256": hashlib.sha256(b"plain capture").hexdigest(),
            "triage_state": "untriaged",
            "triage_reason": None,
        },
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert identity == expected


def test_legacy_capture_still_applies(m3):
    reply = asyncio_run(raw_capture.capture_text(Ev("/x"), "plain capture"))
    assert "Captured canonically on M3" in reply
    assert "object_type" not in m3.sent["capture"]


def test_legacy_response_carrying_object_type_is_rejected(m3):
    def leak(request):
        response = _applied_response(request)
        response["object_type"] = "thought"
        response.pop("response_sha256")
        response["response_sha256"] = m3_client.canonical_hash(response)
        return m3_client.verify_response(response, request)

    m3.builder = leak
    reply = asyncio_run(raw_capture.capture_text(Ev("/x"), "plain capture"))
    assert "Captured canonically" not in reply


def test_typed_and_untyped_same_text_have_distinct_identities():
    typed = capture_schema.validate_text("same words", object_type="thought")
    untyped = capture_schema.validate_text("same words")
    event = Ev("same words")
    assert raw_capture._request_hash(event, typed, "2026-07-17T09:00:00Z") != (
        raw_capture._request_hash(event, untyped, "2026-07-17T09:00:00Z")
    )


# --- replay safety --------------------------------------------------------

def test_replayed_update_does_not_append_twice(m3):
    text = "Don’t let me lose this thought — replay me"
    first = asyncio_run(thoughts.intercept(Ev(text, update_id="u9", message_id="m9")))
    second = asyncio_run(thoughts.intercept(Ev(text, update_id="u9", message_id="m9")))
    assert first == "Saved as a thought."
    assert second == "Saved as a thought."
    # The replay reuses the original request_id, so M3 dedupes rather than
    # appending a second canonical thought.
    assert len({r["request_id"] for r in m3.requests}) == 1


def test_same_update_with_different_thought_is_refused(m3):
    asyncio_run(thoughts.intercept(Ev("Capture this thought: first", update_id="u8")))
    reply = asyncio_run(thoughts.intercept(Ev("Capture this thought: second", update_id="u8")))
    assert reply != "Saved as a thought."


# --- failure never claims success ----------------------------------------

@pytest.mark.parametrize("status,code", [
    ("REJECTED", "capture_schema"),
    ("INDETERMINATE", "m3_indeterminate"),
])
def test_m3_failure_never_says_saved(m3, status, code):
    m3.builder = lambda request: _failure_response(request, status, code)
    reply = asyncio_run(thoughts.intercept(Ev("Capture this thought: x")))
    assert reply == thoughts.UNVERIFIED_REPLY


def test_transport_failure_never_says_saved(m3, monkeypatch):
    async def boom(request_json, *, timeout=12):
        raise m3_client.M3Error("indeterminate_timeout")

    monkeypatch.setattr(m3_client, "call_serialized", boom)
    reply = asyncio_run(thoughts.intercept(Ev("Capture this thought: x")))
    assert reply == thoughts.UNVERIFIED_REPLY


def test_malformed_m3_response_never_says_saved(m3):
    def garbage(request):
        return {"version": 1, "status": "APPLIED", "request_id": request["request_id"]}

    m3.builder = garbage
    reply = asyncio_run(thoughts.intercept(Ev("Capture this thought: x")))
    assert reply == thoughts.UNVERIFIED_REPLY


def test_reply_never_leaks_internals(m3):
    reply = asyncio_run(thoughts.intercept(Ev("Capture this thought: no leaks")))
    assert reply == "Saved as a thought."
    for leak in (CAPTURE_ID, EVENT_ID, "sha256", "receipt", "{", "}"):
        assert leak not in reply


# --- lane bounds ----------------------------------------------------------

def test_other_users_are_not_intercepted(m3):
    event = Ev("Capture this thought: not bill", source=Src(user_id="999", chat_id="999"))
    assert asyncio_run(thoughts.intercept(event)) is None
    assert m3.requests == []


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone


MAX_TEXT_BYTES = 16_384
REQUEST_ENVELOPE_BYTES = 131_072
TRIAGE_STATES = frozenset({"untriaged", "needs_triage", "promoted", "archived"})
# Canonical object types M3 accepts on the additive typed capture shape.
# Legacy captures carry no object_type at all and must never be given one.
TYPED_OBJECT_TYPES = frozenset({"thought"})
_COMMAND = re.compile(
    r"^/(?P<command>capture|capture_needs_triage) (?P<payload>[\s\S]*)$"
)


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCapture:
    original_text: str
    triage_state: str
    triage_reason: str | None
    object_type: str | None = None


def parse_capture(text: str | None) -> ParsedCapture | None:
    """Recognise only explicit, deterministic capture syntax."""
    if not isinstance(text, str):
        return None
    match = _COMMAND.fullmatch(text)
    if not match:
        return None
    needs_triage = match.group("command") == "capture_needs_triage"
    return _validate(
        match.group("payload"),
        "needs_triage" if needs_triage else "untriaged",
        "explicit_needs_triage_control" if needs_triage else None,
    )


def is_capture_command(text: str | None) -> bool:
    """Identify exact capture syntax without loading capture configuration."""
    return isinstance(text, str) and _COMMAND.fullmatch(text) is not None


def _validate(
    payload: str, state: str, reason: str | None, object_type: str | None = None
) -> ParsedCapture:
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CaptureError("capture text is not valid UTF-8") from exc
    if not encoded or not payload.strip():
        raise CaptureError("capture text is empty or whitespace-only")
    if len(encoded) > MAX_TEXT_BYTES:
        raise CaptureError(f"capture text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    if object_type is not None and object_type not in TYPED_OBJECT_TYPES:
        raise CaptureError(f"unsupported object type {object_type!r}")
    return ParsedCapture(payload, state, reason, object_type)


def validate_text(text: str, object_type: str | None = None) -> ParsedCapture:
    """Validate source text supplied by another deterministic gateway route.

    ``object_type`` is omitted entirely for legacy captures; supplying it
    selects M3's additive typed capture shape.
    """
    return _validate(text, "untriaged", None, object_type)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise CaptureError("Telegram source timestamp is missing")
    if value.tzinfo is None:
        raise CaptureError("Telegram source timestamp has no timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

"""PA object validation, transition tables, verbatim wording preservation and
canonical hashing.

Field names and shapes match the accepted M3 candidate (``--pa-object-endpoint``
in ``process-event``) exactly: the object's canonical text is ``wording`` and
``content_sha256`` is the SHA-256 of that wording. Hashing matches the M3
endpoint's ``_ep_canon`` / ``_ep_sha`` byte-for-byte (sorted keys, tight
separators, UTF-8) so the two sides agree.

Wording is preserved *verbatim* — this is a deliberate difference from reminders.
Bill's original words are only stripped of surrounding whitespace; they are never
normalised, re-cased, collapsed or rewritten. The only rejections are the empty
string, an over-long string, or control characters that could corrupt the wire.
"""

from __future__ import annotations

import hashlib
import json
import re

MAX_TEXT = 4000          # UX bound, well under the M3 endpoint's 16384-byte ceiling
MAX_FIELD = 300          # person / project bound (M3 allows 500)

KINDS = ("obligation", "needs_bill")
STATUSES = {
    "obligation": ("open", "waiting", "completed", "cancelled"),
    "needs_bill": ("open", "resolved", "cancelled"),
}
# action -> (allowed-from statuses, resulting status). Exactly mirrors M3.
TRANSITIONS = {
    "obligation": {
        "set_waiting": (("open",), "waiting"),
        "resume": (("waiting",), "open"),
        "complete": (("open", "waiting"), "completed"),
        "cancel": (("open", "waiting"), "cancelled"),
    },
    "needs_bill": {
        "resolve": (("open",), "resolved"),
        "cancel": (("open",), "cancelled"),
    },
}
# Statuses a transition may target from — used to pick the target object.
ELIGIBLE_FROM = {
    ("obligation", "set_waiting"): ("open",),
    ("obligation", "resume"): ("waiting",),
    ("obligation", "complete"): ("open", "waiting"),
    ("obligation", "cancel"): ("open", "waiting"),
    ("needs_bill", "resolve"): ("open",),
    ("needs_bill", "cancel"): ("open",),
}

OBJECT_ID_RE = re.compile(r"(?:obl|ndb)_[0-9a-f]{24}")
ID_PREFIX = {"obligation": "obl_", "needs_bill": "ndb_"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PROJECTION_KEYS = {
    "object_id", "kind", "wording", "person", "due_date", "project",
    "decision", "status", "object_version", "object_sha256", "record_sha256",
}


class ValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj) -> str:
    return sha256(canonical_json(obj))


def preserve_wording(raw: str) -> str:
    """Return Bill's wording exactly as written, stripped of surrounding
    whitespace only. Never normalised, collapsed or re-cased."""
    if not isinstance(raw, str):
        raise ValidationError("wrong_type", "wording")
    text = raw.strip()
    if not text:
        raise ValidationError("empty", "wording")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ValidationError("too_long", f"wording > {MAX_TEXT} bytes")
    if _CONTROL.search(text):
        raise ValidationError("control_character", "wording")
    return text


def clean_field(raw, code: str):
    """A bounded, verbatim optional field (person / project / decision) or None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError("wrong_type", code)
    text = raw.strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > MAX_FIELD:
        text = text[:MAX_FIELD]
    if _CONTROL.search(text):
        raise ValidationError("control_character", code)
    return text


def transition_target_status(kind: str, action: str):
    spec = TRANSITIONS.get(kind, {})
    if action not in spec:
        raise ValidationError("bad_action", f"{kind}/{action}")
    return spec[action][1]


def verify_projection(obj) -> None:
    if not isinstance(obj, dict) or set(obj) != PROJECTION_KEYS:
        raise ValidationError("projection_shape")
    kind = obj.get("kind")
    if kind not in KINDS:
        raise ValidationError("projection_kind")
    oid = obj.get("object_id")
    if not isinstance(oid, str) or not OBJECT_ID_RE.fullmatch(oid or "") or not oid.startswith(ID_PREFIX[kind]):
        raise ValidationError("projection_object_id")
    if obj.get("status") not in STATUSES[kind]:
        raise ValidationError("projection_status")
    if not isinstance(obj.get("object_version"), int) or isinstance(obj.get("object_version"), bool):
        raise ValidationError("projection_version")
    if not isinstance(obj.get("wording"), str) or not obj.get("wording"):
        raise ValidationError("projection_wording")
    for key in ("object_sha256", "record_sha256"):
        if not _HEX64.fullmatch(str(obj.get(key) or "")):
            raise ValidationError("projection_hash")

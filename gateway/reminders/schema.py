"""Reminder payload validation and canonical hashing.

Field names and shapes match the accepted M3 candidate
(``process-event.candidate-reminders-20260719T064236Z``, SHA
``3e88e9b4…269a``) exactly: canonical wording is ``wording``, the absolute due
instant is ``due_at`` (timezone-aware RFC3339), ``content_sha256`` is the SHA-256
of the wording, and ``recurrence`` is null-or-string. Hashing matches the M3
endpoint's ``_ep_canon``/``_ep_sha`` byte-for-byte (sorted keys, tight
separators, UTF-8) so the two sides agree.

The per-action ``reminder`` object is exactly what REMINDER_UPSERT requires:
  create : {wording, content_sha256, due_at, timezone, recurrence, provenance}
  change : {reminder_id, base_version, wording, content_sha256, due_at, timezone, recurrence}
  snooze : {reminder_id, base_version, due_at}
  cancel : {reminder_id, base_version}
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# Williams keeps a UX-tight wording limit well under the M3 endpoint's 16384-byte
# ceiling, so a wording Williams accepts always satisfies M3.
MAX_TEXT = 1000
ACTIONS = frozenset({"create", "change", "snooze", "cancel"})
REMINDER_ID_RE = re.compile(r"rem_[0-9a-f]{24}")
# Canonical reminder statuses returned by the M3 candidate.
STATUSES = frozenset({"scheduled", "cancelled", "delivered", "failed", "uncertain"})

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def clean_text(raw: str) -> str:
    """Validate and normalise the reminder wording to one clean line."""
    if not isinstance(raw, str):
        raise ValidationError("wrong_type", "reminder wording")
    text = unicodedata.normalize("NFC", raw).strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text:
        raise ValidationError("empty", "reminder wording")
    if len(text) > MAX_TEXT:
        raise ValidationError("too_long", f"reminder wording > {MAX_TEXT} chars")
    if _CONTROL.search(text):
        raise ValidationError("control_character", "reminder wording")
    return text


@dataclass(frozen=True)
class ReminderProposal:
    """A single reminder mutation awaiting confirmation, in M3 wire terms."""

    action: str                      # create | change | snooze | cancel
    wording: str                     # canonical wording (echoed; sent to M3)
    due_at: Optional[str]            # tz-aware RFC3339; None only for cancel
    timezone: str = "Australia/Sydney"
    recurrence: Optional[str] = None
    reminder_id: Optional[str] = None    # set for change/snooze/cancel
    base_version: Optional[int] = None   # expected current version for mutations
    provenance: Optional[dict] = None    # required for create only

    def content_sha256(self) -> str:
        return sha256(self.wording)

    def reminder_object(self) -> dict:
        """The exact per-action ``reminder`` object for REMINDER_UPSERT."""
        if self.action == "create":
            return {
                "wording": self.wording,
                "content_sha256": self.content_sha256(),
                "due_at": self.due_at,
                "timezone": self.timezone,
                "recurrence": self.recurrence,
                "provenance": self.provenance,
            }
        if self.action == "cancel":
            return {"reminder_id": self.reminder_id, "base_version": self.base_version}
        if self.action == "snooze":
            return {"reminder_id": self.reminder_id, "base_version": self.base_version,
                    "due_at": self.due_at}
        # change
        return {
            "reminder_id": self.reminder_id,
            "base_version": self.base_version,
            "wording": self.wording,
            "content_sha256": self.content_sha256(),
            "due_at": self.due_at,
            "timezone": self.timezone,
            "recurrence": self.recurrence,
        }

    def local_sha256(self) -> str:
        """A Williams-side digest for staging/replay dedup (not sent to M3)."""
        return canonical_hash({"action": self.action, "reminder": self.reminder_object()})


def validate_action(action: str) -> str:
    if action not in ACTIONS:
        raise ValidationError("bad_action", str(action))
    return action

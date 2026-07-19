"""Natural-language reminder-time resolution.

Reuse-first: this is a thin adapter over ``dateparser`` — we do **not** write a
date parser. The adapter's only jobs are to

  1. resolve explicitly in ``Australia/Sydney`` regardless of the Williams host
     timezone (an explicit ``RELATIVE_BASE`` and ``TIMEZONE`` mean the host
     clock's zone never leaks in);
  2. prefer future dates;
  3. return a timezone-aware RFC3339 value;
  4. ask exactly one clarification when the expression is genuinely ambiguous
     (a bare 1–12 o'clock with no am/pm/day-part, or a date with no time),
     rather than silently guessing a wrong time.

A small, conservative normalisation maps spelled-out clock hours to digits and
turns an adjacent day-part word ("morning"/"evening") into am/pm. That is glue,
not parsing: anything the normaliser and ``dateparser`` cannot resolve returns
``needs_clarification`` and Christine asks one plain question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")

# Spelled-out clock hours we accept in "at <word>" / "<word> o'clock" position.
_WORD_HOUR = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_MERIDIEM = re.compile(r"\b(am|pm|a\.m\.|p\.m\.)\b", re.IGNORECASE)
_DAYPART_AM = re.compile(r"\bmorning\b", re.IGNORECASE)
_DAYPART_PM = re.compile(r"\b(afternoon|evening|tonight|night)\b", re.IGNORECASE)

# A reminder needs an explicit *time*: either a clock time / day-part, or a
# relative duration. Durations accept digits or spelled-out numbers ("two
# minutes") so the real canary "in two minutes" is recognised as timed while a
# bare date like "tomorrow" is not (and prompts one "what time?" question).
_NUM = (r"(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
        r"twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|couple|few|half)")
_CLOCK = re.compile(
    r"(\b\d{1,2}:\d{2}\b)"                              # 09:00
    r"|(\b([1-9]|1[0-2])\s*(am|pm|a\.m\.|p\.m\.)\b)"    # 9am
    r"|(\bat\s+([1-9]|1[0-2])\b)"                       # at 9
    r"|(\b([1-9]|1[0-2])\s*o'?clock\b)"                 # 9 o'clock
    r"|(\b(noon|midday|midnight|morning|afternoon|evening|tonight|night)\b)",
    re.IGNORECASE,
)
_DURATION = re.compile(
    rf"\b(in\s+)?{_NUM}\s*(second|sec|minute|min|hour|hr|day|week)s?\b",
    re.IGNORECASE,
)


def _has_explicit_time(expr: str) -> bool:
    return bool(_CLOCK.search(expr) or _DURATION.search(expr))
# A bare 1–12 hour with "at"/"o'clock" and no am/pm and no day-part word.
_BARE_HOUR = re.compile(r"\bat\s+([1-9]|1[0-2])\b|\b([1-9]|1[0-2])\s*o'?clock\b", re.IGNORECASE)


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one natural time expression."""

    ok: bool
    dt: Optional[datetime] = None            # tz-aware, Australia/Sydney
    normalized: Optional[str] = None
    needs_clarification: bool = False
    reason: Optional[str] = None             # short machine reason for logs/tests


def normalize(expr: str) -> str:
    """Conservative pre-normalisation (glue, not parsing).

    * spelled-out clock hours in "at <word>" / "<word> o'clock" → digits;
    * an explicit hour plus a day-part word → am/pm (and the word is dropped),
      only when no am/pm is already present.
    """
    if not isinstance(expr, str):
        return ""
    s = " " + expr.strip().lower().replace("’", "'") + " "
    for word, num in _WORD_HOUR.items():
        s = re.sub(rf"\bat {word}\b", f"at {num}", s)
        s = re.sub(rf"\b{word} o'?clock\b", f"{num} o'clock", s)

    has_hour = re.search(r"\b([1-9]|1[0-2])\b", s)
    if has_hour and not _MERIDIEM.search(s):
        if _DAYPART_AM.search(s):
            s = re.sub(r"\b(([1-9]|1[0-2])(:\d{2})?)\b(?!\s*(am|pm))", r"\1am", s, count=1)
            s = _DAYPART_AM.sub("", s)
        elif _DAYPART_PM.search(s):
            s = re.sub(r"\b(([1-9]|1[0-2])(:\d{2})?)\b(?!\s*(am|pm))", r"\1pm", s, count=1)
            s = _DAYPART_PM.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_ambiguous(normalized: str) -> bool:
    """True when a clock hour is present but its am/pm is genuinely unclear."""
    if _MERIDIEM.search(normalized):
        return False
    if re.search(r"\b(noon|midday|midnight)\b", normalized):
        return False
    # After normalisation a day-part word would already have injected am/pm, so a
    # surviving bare "at 9"/"9 o'clock" has no way to know morning vs evening.
    return bool(_BARE_HOUR.search(normalized))


def resolve(expr: str, *, now: Optional[datetime] = None) -> Resolution:
    """Resolve ``expr`` to a tz-aware Australia/Sydney datetime, or ask once.

    ``now`` is the resolution base (defaults to the current Sydney wall clock).
    Passing it explicitly — as callers and tests do — is what makes the result
    independent of the Williams host timezone.
    """
    if not isinstance(expr, str) or not expr.strip():
        return Resolution(ok=False, needs_clarification=True, reason="empty")

    base = now.astimezone(SYDNEY) if now is not None else datetime.now(SYDNEY)
    normalized = normalize(expr)

    if _is_ambiguous(normalized):
        return Resolution(ok=False, normalized=normalized,
                          needs_clarification=True, reason="ambiguous_meridiem")

    # dateparser is imported lazily so importing this module never hard-depends
    # on the package being installed (keeps the gate inert-but-importable).
    import dateparser

    dt = dateparser.parse(
        normalized,
        settings={
            "TIMEZONE": "Australia/Sydney",
            "TO_TIMEZONE": "Australia/Sydney",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base.replace(tzinfo=None),
        },
    )
    if dt is None:
        return Resolution(ok=False, normalized=normalized,
                          needs_clarification=True, reason="unparsed")

    dt = dt.astimezone(SYDNEY) if dt.tzinfo else dt.replace(tzinfo=SYDNEY)

    # dateparser fills a missing time-of-day from the base clock, so a bare date
    # ("tomorrow") would silently inherit "now"'s time. If the user never named a
    # time, ask — don't guess a time they didn't choose.
    if not _has_explicit_time(normalized):
        return Resolution(ok=False, normalized=normalized,
                          needs_clarification=True, reason="missing_time")

    # PREFER_DATES_FROM=future covers most cases, but guard hard: a reminder must
    # never resolve into the past.
    if dt <= base:
        return Resolution(ok=False, normalized=normalized,
                          needs_clarification=True, reason="in_past")

    return Resolution(ok=True, dt=dt, normalized=normalized)


def to_rfc3339(dt: datetime) -> str:
    """Timezone-aware RFC3339 string (e.g. ``2026-07-20T09:00:00+10:00``)."""
    return dt.astimezone(SYDNEY).isoformat()


def human(dt: datetime) -> str:
    """Absolute date + time in Sydney for the confirmation echo.

    e.g. ``Monday 20 July 2026 at 9:00 AM (Australia/Sydney)``.
    """
    local = dt.astimezone(SYDNEY)
    hour12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.strftime("%M")
    ampm = local.strftime("%p")
    day = str(int(local.strftime("%d")))
    return (
        f"{local.strftime('%A')} {day} {local.strftime('%B %Y')} "
        f"at {hour12}:{minute} {ampm} (Australia/Sydney)"
    )

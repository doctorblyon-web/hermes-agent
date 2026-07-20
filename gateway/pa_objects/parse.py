"""Conservative natural-language recognition for PA objects.

Recognition is deliberately narrow so ordinary conversation always falls through:
only clearly-marked obligation / decision statements and their explicit transition
commands are matched. Nothing here rewrites Bill's words — the wording handed on
for canonical storage is the exact substring he wrote.

Date resolution reuses the reminders ``dateparser`` adapter's zone discipline
(explicit Australia/Sydney, future-preferred, host-timezone-independent) but
resolves to a *calendar date* — an obligation due date has no time-of-day and one
is never invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")

# --- creation recognition ----------------------------------------------------

# "I need to / I have to / I must / I've got to / I ought to <body>"
_CREATE_LEAD = re.compile(
    r"^\s*i\s+(?:need\s+to|have\s+to|must|ought\s+to|(?:'ve|ve|have)\s+got\s+to|got\s+to|gotta)\s+(?P<body>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
# A body that is really a decision request -> needs_bill, not an obligation.
_DECISION_BODY = re.compile(
    r"^\s*(?:decide|make\s+a\s+decision|figure\s+out\s+whether|work\s+out\s+whether|choose\s+whether|"
    r"decide\s+whether|settle\s+whether)\b",
    re.IGNORECASE,
)

# Person the obligation is directed at ("send Gina ...", "reply to Sam ...").
_PERSON = re.compile(
    r"\b(?:send|tell|email|e-mail|call|phone|give|pay|ask|remind|update|meet|thank|"
    r"reply\s+to|get\s+back\s+to|write\s+to|follow\s+up\s+with|check\s+with|chase|invoice)\s+"
    r"(?P<person>[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)?)\b"
)
# Trailing due phrase ("... by Friday", "... before Monday", "... due next week").
_DUE = re.compile(
    r"\b(?:by\s+the\s+end\s+of|by\s+end\s+of|by|before|due(?:\s+on|\s+by)?)\s+(?P<when>[^.,;]+?)\s*(?:[.,;]|$)",
    re.IGNORECASE,
)
# A modest, explicit project phrase only.
_PROJECT = re.compile(
    r"\bfor\s+the\s+(?P<project>[A-Za-z0-9][\w &'’-]{0,60}?)\s+project\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Creation:
    kind: str                    # "obligation" | "needs_bill"
    wording: str                 # verbatim body Bill wrote
    person: Optional[str] = None
    due_date: Optional[str] = None   # YYYY-MM-DD (Australia/Sydney) or None
    project: Optional[str] = None


def parse_create(text: str, *, now: Optional[datetime] = None) -> Optional[Creation]:
    """Recognise an obligation or needs_bill creation, or return None."""
    if not isinstance(text, str):
        return None
    m = _CREATE_LEAD.match(text)
    if not m:
        return None
    body = m.group("body").strip()
    if not body:
        return None
    if _DECISION_BODY.match(body):
        return Creation(kind="needs_bill", wording=body)
    person = _first_person(body)
    due_date = _first_due_date(body, now=now)
    pm = _PROJECT.search(body)
    project = pm.group("project").strip() if pm else None
    return Creation(kind="obligation", wording=body, person=person,
                    due_date=due_date, project=project)


def _first_person(body: str) -> Optional[str]:
    m = _PERSON.search(body)
    return m.group("person").strip() if m else None


def _first_due_date(body: str, *, now=None) -> Optional[str]:
    """Resolve the last well-formed due phrase to a Sydney date, else None."""
    resolved = None
    for m in _DUE.finditer(body):
        d = resolve_due_date(m.group("when").strip(), now=now)
        if d is not None:
            resolved = d
    return resolved.isoformat() if resolved else None


def resolve_due_date(expr: str, *, now: Optional[datetime] = None) -> Optional[date]:
    """Resolve a natural date expression to a calendar date in Australia/Sydney.

    Future-preferred and independent of the Williams host timezone. Returns None
    when the expression does not resolve to a date — a due date is never invented.
    """
    if not isinstance(expr, str) or not expr.strip():
        return None
    base = now.astimezone(SYDNEY) if now is not None else datetime.now(SYDNEY)
    try:
        import dateparser
    except Exception:
        return None
    dt = dateparser.parse(
        expr,
        settings={
            "TIMEZONE": "Australia/Sydney",
            "TO_TIMEZONE": "Australia/Sydney",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base.replace(tzinfo=None),
        },
    )
    if dt is None:
        return None
    local = dt.astimezone(SYDNEY) if dt.tzinfo else dt.replace(tzinfo=SYDNEY)
    return local.date()


def human_date(iso_date: str) -> str:
    """'Friday 24 July 2026 (Australia/Sydney)' for a YYYY-MM-DD string."""
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    day = str(int(d.strftime("%d")))
    return f"{d.strftime('%A')} {day} {d.strftime('%B %Y')} (Australia/Sydney)"


# --- transition recognition --------------------------------------------------

@dataclass(frozen=True)
class Transition:
    kind: str                    # "obligation" | "needs_bill"
    action: str                  # set_waiting | resume | complete | cancel | resolve
    ref: Optional[str] = None    # person / wording hint to pick the target, or None
    decision: Optional[str] = None   # needs_bill resolve only (verbatim)


_WAITING = re.compile(
    r"^\s*(?:i'?m|i\s+am)\s+waiting\s+(?:for|on)\s+(?P<ref>[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)?)\b",
    re.IGNORECASE,
)
_RESUME = re.compile(
    r"\bresume\s+(?:that|the|this|my)\s+obligation\b|\bresume\s+the\s+(?P<ref>.+?)\s+obligation\b",
    re.IGNORECASE,
)
_COMPLETE = re.compile(
    r"\b(?:mark|set)\s+(?:the\s+|my\s+)?(?P<ref1>.+?)\s+obligation\s+(?:as\s+)?(?:complete|completed|done|finished)\b"
    r"|\b(?:complete|finish)\s+(?:the\s+|my\s+)?(?P<ref2>.+?)\s+obligation\b"
    r"|\b(?:the|my)\s+(?P<ref3>.+?)\s+obligation\s+is\s+(?:complete|completed|done|finished)\b"
    r"|\b(?:mark|set)\s+(?:that|this)\s+obligation\s+(?:as\s+)?(?:complete|completed|done|finished)\b",
    re.IGNORECASE,
)
_CANCEL_OBL = re.compile(
    r"\bcancel\s+(?:that|the|this|my)\s+obligation\b|\bcancel\s+the\s+(?P<ref>.+?)\s+obligation\b",
    re.IGNORECASE,
)
_RESOLVE = re.compile(
    r"^\s*resolve\s+(?:that|the|this|my)\s+decision\s*[:\-]\s*(?P<decision>.+?)\s*$"
    r"|^\s*resolve\s+the\s+(?P<ref>.+?)\s+decision\s*[:\-]\s*(?P<decision2>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL_DEC = re.compile(
    r"\bcancel\s+(?:that|the|this|my)\s+decision\b|\bcancel\s+the\s+(?P<ref>.+?)\s+decision\b",
    re.IGNORECASE,
)


def _clean_ref(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    ref = re.sub(r"\b(the|my|a|an|that|this|obligation|decision|about|for|to)\b", " ", ref, flags=re.IGNORECASE)
    ref = re.sub(r"\s+", " ", ref).strip()
    return ref or None


def parse_transition(text: str) -> Optional[Transition]:
    """Recognise an explicit status transition, or return None. No inference:
    only clear, explicit commands transition an object."""
    if not isinstance(text, str) or not text.strip():
        return None

    m = _RESOLVE.match(text)
    if m:
        decision = (m.group("decision") or m.group("decision2") or "").strip()
        if decision:
            return Transition(kind="needs_bill", action="resolve",
                              ref=_clean_ref(m.group("ref")), decision=decision)

    if _CANCEL_DEC.search(text):
        m = _CANCEL_DEC.search(text)
        return Transition(kind="needs_bill", action="cancel", ref=_clean_ref(m.group("ref")))

    m = _WAITING.match(text)
    if m:
        return Transition(kind="obligation", action="set_waiting", ref=_clean_ref(m.group("ref")))

    m = _RESUME.search(text)
    if m:
        return Transition(kind="obligation", action="resume", ref=_clean_ref(m.groupdict().get("ref")))

    m = _COMPLETE.search(text)
    if m:
        ref = m.group("ref1") or m.group("ref2") or m.group("ref3")
        return Transition(kind="obligation", action="complete", ref=_clean_ref(ref))

    m = _CANCEL_OBL.search(text)
    if m:
        return Transition(kind="obligation", action="cancel", ref=_clean_ref(m.group("ref")))

    return None

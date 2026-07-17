"""Closed request and filtered response model for dashboard_status."""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo


SECTIONS = ("signal", "missions", "captures", "needs_bill", "reminders", "health")
EXECUTIVE_BRIEF = "executive_brief"
REQUEST_SECTIONS = frozenset(("all", EXECUTIVE_BRIEF) + SECTIONS)
OUTPUT_BYTES_MAX = 128 * 1024
MAX_MISSIONS = 20
MAX_CAPTURES = 5
MAX_BLOCKERS = 20
MAX_CAPTURE_TEXT_BYTES = 16 * 1024
MAX_MISSION_TEXT_BYTES = 2 * 1024
MAX_STRING_BYTES = 1024
CAPTURE_FRESH_SECONDS = 24 * 60 * 60
HEALTH_CLOCK_SKEW_SECONDS = 60

MISSION_TITLES = {
    "signalpath": "SignalPath",
    "knowledgebrains": "KnowledgeBrains",
    "billos": "BillOS",
    "legal": "Legal",
    "investment": "Investment",
}

_PROTECTED_TEXT_RE = re.compile(
    r"(?:"
    r"\b(?:canary[A-Za-z0-9_-]*|receipt|credential|api[_ -]?key|token|launchctl)\b|"
    r"\b(?:id|identifier)\s*[:#-]?\s*[A-Za-z0-9_-]+\b|"
    r"\b(?:localhost|host(?:name)?\s+[A-Za-z0-9.-]+)\b|"
    r"\b(?:plugin|formatter|schema|client|server|function|class|service|endpoint)\b|"
    r"\b[A-Za-z][A-Za-z0-9]*(?:[-_.](?:event|server|status|set|tick))\b|"
    r"\b(?:tas|rem|cap|sig|evt|rcpt|receipt|task|capture|mission)[_-][A-Za-z0-9_-]+\b|"
    r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b|"
    r"\b[0-9a-fA-F]{32,64}\b|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b|"
    r"https?://|(?:^|\s)/[^\s]+|[A-Za-z]:\\|"
    r"\b(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}\b"
    r")",
    re.IGNORECASE,
)
_IMPLEMENTATION_CASE_RE = re.compile(
    r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+)\b"
)


def _safe_user_text(value: Any) -> str:
    """Return a whole safe value or a fixed non-revealing replacement."""
    text = _bounded_text(value, MAX_MISSION_TEXT_BYTES)
    if text is None or _PROTECTED_TEXT_RE.search(text) or _IMPLEMENTATION_CASE_RE.search(text):
        return "Details withheld."
    return text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _bounded_text(value: Any, maximum: int = MAX_STRING_BYTES) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        return None
    return value


def validate_request(args: Any) -> str:
    if args is None:
        args = {}
    if not isinstance(args, dict) or set(args) - {"section"}:
        raise ValueError("dashboard_status accepts only the fixed section argument")
    section = args.get("section", "all")
    if not isinstance(section, str) or section not in REQUEST_SECTIONS:
        raise ValueError("unsupported dashboard_status section")
    return section


def _envelope(*, availability: str, source_as_of: str | None, freshness: str,
              items: list | None = None, value: Any = None,
              error_reason: str | None = None) -> dict[str, Any]:
    return {
        "availability": availability,
        "source_as_of": source_as_of,
        "freshness": freshness,
        "items": items,
        "value": value,
        "error_reason": error_reason,
    }


def _not_implemented() -> dict[str, Any]:
    return _envelope(
        availability="not_implemented", source_as_of=None,
        freshness="unknown", items=[], error_reason=None,
    )


def _unavailable(code: str) -> dict[str, Any]:
    return _envelope(
        availability="unavailable", source_as_of=None,
        freshness="unknown", items=[], error_reason=code[:160],
    )


def _signal(raw: Any, now: datetime) -> dict[str, Any]:
    if raw is None:
        return _envelope(
            availability="live", source_as_of=None, freshness="unknown",
            value={"date": None, "actions": [], "no_today": None},
        )
    if not isinstance(raw, dict):
        return _envelope(
            availability="partial", source_as_of=None, freshness="unknown",
            value={"date": None, "actions": [], "no_today": None},
            error_reason="invalid_signal",
        )
    signal_date = raw.get("date")
    try:
        parsed_signal_date = date.fromisoformat(signal_date) if isinstance(signal_date, str) else None
    except ValueError:
        parsed_signal_date = None
    updated = _timestamp(raw.get("updated_at"))
    source_as_of = _iso(updated) if updated else None
    today = now.astimezone(ZoneInfo("Australia/Sydney")).date().isoformat()
    if parsed_signal_date is None:
        return _envelope(
            availability="partial", source_as_of=source_as_of, freshness="unknown",
            value={"date": None, "actions": [], "no_today": None},
            error_reason="invalid_signal_date",
        )
    freshness = "fresh" if signal_date == today and updated else (
        "stale" if isinstance(signal_date, str) and signal_date != today else "unknown"
    )
    if signal_date != today:
        return _envelope(
            availability="live", source_as_of=source_as_of, freshness=freshness,
            value={"date": None, "actions": [], "no_today": None},
        )
    actions = raw.get("actions")
    no_today = raw.get("no_today")
    if not isinstance(actions, list) or len(actions) > 3 or not isinstance(no_today, (str, type(None))):
        return _envelope(
            availability="partial", source_as_of=source_as_of, freshness=freshness,
            value={"date": signal_date, "actions": [], "no_today": None},
            error_reason="invalid_signal",
        )
    filtered: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("done"), bool):
            return _envelope(
                availability="partial", source_as_of=source_as_of, freshness=freshness,
                value={"date": signal_date, "actions": [], "no_today": None},
                error_reason="invalid_signal_action",
            )
        verb = _bounded_text(action.get("verb"))
        obj = _bounded_text(action.get("object"))
        mission = _bounded_text(action.get("source_mission"))
        if verb is None or obj is None or mission is None:
            return _envelope(
                availability="partial", source_as_of=source_as_of, freshness=freshness,
                value={"date": signal_date, "actions": [], "no_today": None},
                error_reason="invalid_signal_action",
            )
        filtered.append({
            "text": verb + ((" " + obj) if obj else ""),
            "mission": mission,
            "done": action["done"],
        })
    safe_no_today = _bounded_text(no_today) if no_today is not None else None
    if no_today is not None and safe_no_today is None:
        return _envelope(
            availability="partial", source_as_of=source_as_of, freshness=freshness,
            value={"date": signal_date, "actions": filtered, "no_today": None},
            error_reason="invalid_signal_no_today",
        )
    return _envelope(
        availability="live", source_as_of=source_as_of, freshness=freshness,
        value={"date": signal_date, "actions": filtered, "no_today": safe_no_today},
    )


def _mission(item: Any, now: datetime) -> dict[str, Any]:
    base = {
        "availability": "partial", "source_as_of": None, "freshness": "unknown",
        "mission_id": None, "title": None, "where_we_are": None,
        "last_movement": None, "next_action": None, "blockers": [],
        "status": None, "error_reason": "invalid_mission",
    }
    if not isinstance(item, dict):
        return base
    mission_id = _bounded_text(item.get("mission_id"))
    status = _bounded_text(item.get("status"))
    where = _bounded_text(item.get("where_we_are"), MAX_MISSION_TEXT_BYTES)
    next_action = _bounded_text(item.get("next_action"))
    updated = _timestamp(item.get("updated_at"))
    movement = item.get("last_movement")
    blockers = item.get("blockers")
    stale_after = item.get("stale_after_days")
    if (
        mission_id is None or status is None or where is None or next_action is None
        or not isinstance(movement, dict) or not isinstance(blockers, list)
        or len(blockers) > MAX_BLOCKERS or isinstance(stale_after, bool)
        or not isinstance(stale_after, int) or stale_after < 0 or stale_after > 3650
    ):
        base.update({
            "mission_id": mission_id, "title": MISSION_TITLES.get(mission_id, mission_id),
            "source_as_of": _iso(updated) if updated else None,
        })
        return base
    movement_date = movement.get("date")
    movement_what = _bounded_text(movement.get("what"))
    try:
        moved = date.fromisoformat(movement_date) if isinstance(movement_date, str) else None
    except ValueError:
        moved = None
    safe_blockers = [_bounded_text(value) for value in blockers]
    if moved is None or movement_what is None or any(value is None for value in safe_blockers):
        base.update({
            "mission_id": mission_id, "title": MISSION_TITLES.get(mission_id, mission_id),
            "source_as_of": _iso(updated) if updated else None,
        })
        return base
    today = now.astimezone(ZoneInfo("Australia/Sydney")).date()
    freshness = "stale" if (today - moved).days > stale_after else "fresh"
    return {
        "availability": "live", "source_as_of": _iso(updated) if updated else None,
        "freshness": freshness if updated else "unknown",
        "mission_id": mission_id, "title": MISSION_TITLES.get(mission_id, mission_id),
        "where_we_are": where,
        "last_movement": {"date": movement_date, "what": movement_what},
        "next_action": next_action, "blockers": safe_blockers,
        "status": status, "error_reason": None,
    }


def _missions(raw: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, list) or len(raw) > MAX_MISSIONS:
        return _unavailable("invalid_missions")
    items = [_mission(item, now) for item in raw]
    availability = "partial" if any(item["availability"] == "partial" for item in items) else "live"
    states = {item["freshness"] for item in items}
    freshness = "stale" if "stale" in states else ("unknown" if "unknown" in states else "fresh")
    return _envelope(
        availability=availability, source_as_of=None, freshness=freshness,
        items=items, error_reason="invalid_mission" if availability == "partial" else None,
    )


def _captures(raw: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _unavailable("invalid_captures")
    recent = raw.get("recent")
    status = raw.get("status")
    canonical = _timestamp(raw.get("canonical_as_of"))
    if not isinstance(recent, list) or not isinstance(status, str):
        return _unavailable("invalid_captures")
    filtered: list[tuple[datetime, dict[str, Any]]] = []
    partial = False
    for item in recent:
        if not isinstance(item, dict):
            partial = True
            continue
        text = _bounded_text(item.get("original_text"), MAX_CAPTURE_TEXT_BYTES)
        captured_at = _bounded_text(item.get("captured_at"), 64)
        capture_id = _bounded_text(item.get("id"), 256)
        source = _bounded_text(item.get("source"), 128)
        receipt_status = _bounded_text(item.get("receipt_status"), 128)
        triage_state = _bounded_text(item.get("triage_state"), 128)
        captured_timestamp = _timestamp(captured_at)
        if None in (text, captured_at, capture_id, source, receipt_status, triage_state) or captured_timestamp is None:
            partial = True
            continue
        filtered.append((captured_timestamp, {
            "id": capture_id, "text": text, "captured_at": captured_at,
            "source": source, "receipt_status": receipt_status,
            "triage_state": triage_state,
        }))
    filtered.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
    items = [item for _, item in filtered[:MAX_CAPTURES]]
    freshness = "unknown" if canonical is None else (
        "fresh" if abs((now - canonical).total_seconds()) <= CAPTURE_FRESH_SECONDS else "stale"
    )
    if status != "ok":
        partial = True
    return _envelope(
        availability="partial" if partial else "live",
        source_as_of=_iso(canonical) if canonical else None,
        freshness=freshness, items=items,
        error_reason="capture_projection_degraded" if partial else None,
    )


def _health(raw_timestamp: Any, projection_read_at: str, now: datetime) -> dict[str, Any]:
    source = _timestamp(raw_timestamp)
    read_at = _timestamp(projection_read_at)
    if source is None or read_at is None:
        return _envelope(
            availability="partial", source_as_of=None, freshness="unknown",
            value={"canonical_reachable": True, "projection_status": "degraded", "last_successful_read": projection_read_at},
            error_reason="invalid_projection_timestamp",
        )
    freshness = "fresh" if abs((read_at - source).total_seconds()) <= HEALTH_CLOCK_SKEW_SECONDS else "stale"
    return _envelope(
        availability="live", source_as_of=_iso(source), freshness=freshness,
        value={"canonical_reachable": True, "projection_status": "ok", "last_successful_read": projection_read_at},
    )


def _selected(section: str) -> tuple[str, ...]:
    return SECTIONS if section == "all" else (section,)


def _priority_brief(signal: dict[str, Any]) -> tuple[str, list[str]]:
    value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
    actions = value.get("actions") if isinstance(value.get("actions"), list) else []
    availability = signal.get("availability")
    if availability == "unavailable":
        status = "unavailable"
    elif availability == "partial":
        status = "partial"
    elif value.get("date") is None:
        status = "not_set"
    elif actions:
        status = "set"
    elif value.get("no_today") is not None:
        status = "no_today"
    else:
        status = "not_set"
    safe_actions = [
        _safe_user_text(action["text"]) for action in actions
        if isinstance(action, dict) and isinstance(action.get("text"), str)
    ] if status == "set" else []
    message = None if status == "set" else (
        "No priorities are set for today."
        if status in {"not_set", "no_today"} else
        "Today’s priorities are only partially available."
        if status == "partial" else
        "Today’s priorities are unavailable."
    )
    return status, safe_actions if status == "set" else [message]


def _executive_brief(upstream: dict[str, Any], projection_read_at: str,
                     now: datetime) -> dict[str, Any]:
    signal = _signal(upstream.get("signal"), now)
    missions = _missions(upstream.get("missions"), now)
    blockers: list[dict[str, str]] = []
    stale_missions: list[str] = []
    for mission in missions.get("items") or []:
        if not isinstance(mission, dict):
            continue
        mission_id = mission.get("mission_id")
        title = MISSION_TITLES.get(mission_id)
        if title is None:
            continue
        for blocker in mission.get("blockers") or []:
            blockers.append({"mission_title": title, "text": _safe_user_text(blocker)})
        if mission.get("freshness") == "stale":
            stale_missions.append(title)

    priority_status, priority_lines = _priority_brief(signal)
    rendered_priorities = (
        [f"- {line}" for line in priority_lines]
        if priority_status == "set" else priority_lines
    )
    blocker_lines = [f"- {item['mission_title']}: {item['text']}" for item in blockers]
    stale_lines = [f"- {title}" for title in stale_missions]
    brief = "\n".join([
        "Today's Priorities",
        *rendered_priorities,
        "",
        "Current Blockers",
        *(blocker_lines or ["- No current blockers."]),
        "",
        "Stale Mission Information",
        *(stale_lines or ["- No mission information is stale."]),
    ])
    return {"brief": brief}


def _overall(sections: dict[str, dict[str, Any]]) -> str:
    material = [value for value in sections.values() if value["availability"] != "not_implemented"]
    if material and all(value["availability"] == "unavailable" for value in material):
        return "unavailable"
    if any(
        value["availability"] in {"partial", "unavailable"}
        or value["freshness"] in {"stale", "unknown"}
        for value in material
    ):
        return "degraded"
    return "ok"


def build_snapshot(upstream: dict[str, Any], section: str, *, projection_read_at: str,
                   now: datetime | None = None) -> dict[str, Any]:
    validate_request({"section": section})
    if not isinstance(upstream, dict):
        raise ValueError("invalid projection")
    if not {"signal", "missions", "captures", "timestamp"}.issubset(upstream):
        raise ValueError("projection missing required fields")
    now = (now or _now()).astimezone(timezone.utc)
    if section == EXECUTIVE_BRIEF:
        return _executive_brief(upstream, projection_read_at, now)
    builders = {
        "signal": lambda: _signal(upstream.get("signal"), now),
        "missions": lambda: _missions(upstream.get("missions"), now),
        "captures": lambda: _captures(upstream.get("captures"), now),
        "needs_bill": _not_implemented,
        "reminders": _not_implemented,
        "health": lambda: _health(upstream.get("timestamp"), projection_read_at, now),
    }
    sections = {name: builders[name]() for name in _selected(section)}
    return {
        "generated_at": _iso(now), "projection_read_at": projection_read_at,
        "overall_status": _overall(sections), "sections": sections,
    }


def build_failure(section: str, code: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = (now or _now()).astimezone(timezone.utc)
    if section == EXECUTIVE_BRIEF:
        return {"brief": "\n".join([
            "Today's Priorities", "Today’s priorities are unavailable.", "",
            "Current Blockers", "- Current blockers are unavailable.", "",
            "Stale Mission Information", "- Stale mission information is unavailable.",
        ])}
    sections = {
        name: (_not_implemented() if name in {"needs_bill", "reminders"} else _unavailable(code))
        for name in _selected(section)
    }
    return {
        "generated_at": _iso(now), "projection_read_at": None,
        "overall_status": _overall(sections), "sections": sections,
    }

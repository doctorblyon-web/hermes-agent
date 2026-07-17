"""Read-only BillOS dashboard status plugin."""

from __future__ import annotations

import json
from typing import Any

from plugins.dashboard_status.client import DashboardClientError, fetch_state
from plugins.dashboard_status.schema import (
    OUTPUT_BYTES_MAX,
    build_failure,
    build_snapshot,
    validate_request,
)


DASHBOARD_STATUS_SCHEMA = {
    "name": "dashboard_status",
    "description": (
        "Read the bounded user-facing BillOS dashboard snapshot or one fixed section. "
        "For any question about today's priorities, current blockers, or stale mission "
        "information, you MUST use section='executive_brief'. That section returns a "
        "complete deterministic brief: reproduce its brief value without adding, "
        "removing, reclassifying, or editorialising. Use section='all' only when the "
        "user explicitly requests the full dashboard snapshot. This tool is read-only "
        "and cannot mutate or administer anything."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "section": {
                "type": "string",
                "description": (
                    "Use executive_brief for priorities, blockers, or stale missions; "
                    "return its brief verbatim. Other values expose individual diagnostic sections."
                ),
                "enum": [
                    "all", "signal", "missions", "captures",
                    "needs_bill", "reminders", "health", "executive_brief",
                ],
                "default": "all",
            }
        },
    },
}


def handle_dashboard_status(args: Any, **_: Any) -> str:
    """Return a bounded filtered snapshot; never return cached dashboard data."""
    try:
        section = validate_request(args)
    except ValueError as exc:
        return json.dumps(
            {"success": False, "error": str(exc)},
            sort_keys=True,
            separators=(",", ":"),
        )

    try:
        upstream, projection_read_at = fetch_state()
        result = build_snapshot(upstream, section, projection_read_at=projection_read_at)
    except DashboardClientError as exc:
        result = build_failure(section, exc.code)
    except (TypeError, ValueError, UnicodeError):
        result = build_failure(section, "invalid_projection")

    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > OUTPUT_BYTES_MAX:
        encoded = json.dumps(
            build_failure(section, "output_too_large"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return encoded


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="dashboard_status",
        toolset="dashboard_status",
        schema=DASHBOARD_STATUS_SCHEMA,
        handler=handle_dashboard_status,
        emoji="📊",
    )

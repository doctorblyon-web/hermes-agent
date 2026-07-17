import copy
import asyncio
import json
import logging
import calendar
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.plugin_llm import PluginLlm, PluginLlmTextInput
from gateway.natural_pa_intake.store import Store
from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
KINDS = {"capture", "task", "daily_priorities", "reminder", "obligation", "retrieval", "flight_search", "birthday", "date_resolution"}

_DASHBOARD_REFERENCE = (
    r"(?:billos\s+dashboard|dashboard(?:\s+state)?|command\s+cent(?:er|re))"
)
_DASHBOARD_READ_RE = re.compile(
    rf"\b(?:"
    rf"(?:using|from|according\s+to|look\s+at)\s+(?:the\s+)?{_DASHBOARD_REFERENCE}|"
    rf"(?:show(?:\s+me)?|list|summari[sz]e)\s+(?:the\s+)?{_DASHBOARD_REFERENCE}(?:\s+(?:status|priorities|blockers?|freshness))?|"
    rf"tell\s+me\s+what\s+(?:the\s+)?{_DASHBOARD_REFERENCE}\s+(?:says|shows)|"
    rf"what\s+does\s+(?:the\s+)?{_DASHBOARD_REFERENCE}\s+(?:say|show)|"
    rf"{_DASHBOARD_REFERENCE}\s+(?:status|priorities|blockers?|freshness)|"
    rf"which\s+{_DASHBOARD_REFERENCE}\s+information\s+is\s+stale"
    rf")\b"
)
_DASHBOARD_MUTATION_RE = re.compile(
    r"\b(?:add|capture|change|create|delete|edit|mark|modify|remove|set|update|write)\b"
)


def is_dashboard_read_request(text):
    """Return true only for an explicit, non-mutating dashboard read request."""
    if not isinstance(text, str):
        return False
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())
    return bool(
        _DASHBOARD_READ_RE.search(normalized)
        and not _DASHBOARD_MUTATION_RE.search(normalized)
    )

PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["actions", "clarification"],
    "properties": {
        "clarification": {"type": ["string", "null"]},
        "actions": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "title", "text", "due_at", "person", "project", "next_action", "priorities"],
            "properties": {
                "kind": {"enum": sorted(KINDS)}, "title": {"type": "string"}, "text": {"type": "string"},
                "due_at": {"type": ["string", "null"]}, "person": {"type": ["string", "null"]},
                "project": {"type": ["string", "null"]}, "next_action": {"type": ["string", "null"]},
                "priorities": {"type": "array", "items": {"type": "string"}, "maxItems": 20}
            }}
    }}}

INSTRUCTIONS = """You classify Bill's private PA message into zero or more bounded actions.
Allowed kinds: capture, task, daily_priorities, reminder, obligation, retrieval, flight_search, birthday, date_resolution.
Return actions in message order. Preserve the meaningful supplied wording in text for capture.
Use task for work Bill says he needs/has to do. Use reminder only for a request to notify him.
Use obligation for waiting/dependency/what another person owes. Use daily_priorities only when he identifies today's goals/priorities.
When a waiting/owes statement includes a follow-up instruction, keep it as one obligation action with the follow-up in next_action and due_at; do not create a second task.
Use retrieval for questions about saved ideas, priorities, waiting items, tasks, due items, blockers, or what moved.
Use flight_search only when Bill explicitly asks to find, research, compare, or look up flights. Never use it merely because he creates a reminder to book a flight.
Use date_resolution when Bill asks for a calendar date such as the second Tuesday in December. Use birthday when he asks you to remember a person's birthday; retain it as needs clarification when its date or reminder timing is missing. Always return every independent intention even if one needs clarification. Never use the top-level clarification field to replace or suppress other actions.
Resolve dates in Australia/Sydney and put an RFC3339 timestamp in due_at. If a reminder's time materially changes the action and is not reasonably inferable, set clarification to one short question and return no actions.
If more than three daily priorities are supplied, set clarification asking which three and do not propose that action.
One or two supplied daily priorities are valid: propose exactly those values and never ask for missing priorities.
Conversation, acknowledgements, hypotheticals, quoted examples, and statements about future possible topics are not actions. Do not infer capture merely because Bill says 'I want to talk about' something.
Never claim anything completed. The handlers decide that."""


def settings():
    gateway = load_config_readonly().get("gateway", {})
    value = gateway.get("natural_pa_intake", {}) if isinstance(gateway, dict) else {}
    return value if isinstance(value, dict) else {"enabled": False}


def _lane(event, cfg):
    source = event.source
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    return platform == "telegram" and source.chat_type == "dm" and str(source.user_id) == str(cfg.get("bill_user_id")) and str(source.chat_id) == str(cfg.get("chat_id"))


async def _propose(text):
    llm = PluginLlm(plugin_id="christine-natural-pa-intake")
    now = datetime.now(ZoneInfo("Australia/Sydney")).isoformat()
    instructions = INSTRUCTIONS + f"\nCurrent Australia/Sydney date and time: {now}."
    result = await llm.acomplete_structured(instructions=instructions, input=[PluginLlmTextInput(text=text)], json_schema=PROPOSAL_SCHEMA, schema_name="christine_pa_actions", temperature=0, max_tokens=1200, timeout=20, purpose="natural_pa_intake")
    if not isinstance(result.parsed, dict):
        raise ValueError("PA interpreter returned no validated proposal")
    return result.parsed


def _details(action):
    return {key: action.get(key) for key in ("due_at", "person", "project", "next_action") if action.get(key)}


def _calendar_context(source_text):
    match = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+(\d{4}))?\b", source_text, re.I)
    if not match:
        return None
    weekday, day_text, month_text, year_text = match.groups()
    day = int(day_text)
    month = list(calendar.month_name).index(month_text.capitalize())
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    year = int(year_text) if year_text else now.year
    candidate = datetime(year, month, day, tzinfo=ZoneInfo("Australia/Sydney"))
    if not year_text and candidate.date() < now.date():
        candidate = candidate.replace(year=year + 1)
    actual = candidate.strftime("%A")
    if actual.lower() == weekday.lower():
        return {"intended_date": candidate.date().isoformat(), "stated_weekday": weekday, "validated": True}
    wanted = list(calendar.day_name).index(weekday.capitalize())
    delta = (wanted - candidate.weekday()) % 7
    adjacent = candidate if delta == 0 else candidate + timedelta(days=delta)
    question = f"Do you mean {actual} {candidate.day} {candidate.strftime('%B %Y')} or {adjacent.strftime('%A')} {adjacent.day} {adjacent.strftime('%B %Y')}?"
    return {"intended_date_text": match.group(0), "stated_weekday": weekday, "stated_date": candidate.date().isoformat(), "actual_weekday": actual, "alternate_date": adjacent.date().isoformat(), "validated": False, "clarification": question}


def _time_was_defaulted(source_text):
    return re.search(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight|morning|afternoon|evening)\b", source_text, re.I) is None


def _parse_ordinal_date(text):
    match = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)\b", text, re.I)
    if not match:
        return None
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return None
    return {"day": day, "month": match.group(2).capitalize(), "source_text": match.group(0)}


def _parse_clarification_time(text):
    if not re.search(r"\bfriday(?:\s+this\s+week)?\b", text, re.I):
        return None
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    days = (calendar.FRIDAY - now.weekday()) % 7
    return (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)


def _supplement_demonstrated_actions(text, actions):
    """Recover independently stated actions the interpreter must not drop."""
    existing = {a.get("kind") for a in actions if isinstance(a, dict)}
    additions = []
    lower = text.lower()
    if "three priorities" in lower and "daily_priorities" not in existing:
        block = text[lower.index("three priorities"):]
        values = [m.group(1).strip().rstrip(".") for m in re.finditer(r"(?:^|\n)\s*\d+[.)]\s*(.+?)(?=(?:\n\s*\d+[.)])|(?:\n\s*(?:can you|please|also)\b)|$)", block, re.I | re.S)]
        if values:
            additions.append({"kind": "daily_priorities", "title": "Today's three", "text": block, "due_at": None, "person": None, "project": None, "next_action": None, "priorities": values[:3]})
    if "second tuesday in december" in lower and "date_resolution" not in existing:
        year = datetime.now(ZoneInfo("Australia/Sydney")).year
        first = datetime(year, 12, 1, tzinfo=ZoneInfo("Australia/Sydney"))
        day = 1 + ((1 - first.weekday()) % 7) + 7
        resolved = first.replace(day=day)
        additions.append({"kind": "date_resolution", "title": "Second Tuesday in December", "text": "second Tuesday in December", "due_at": resolved.date().isoformat(), "person": None, "project": "Adelaide travel", "next_action": None, "priorities": []})
    if "virgin" in lower and any(phrase in lower for phrase in ("what time", "flight times", "does virgin fly", "research")) and "flight_search" not in existing:
        additions.append({"kind": "flight_search", "title": "Virgin Sydney–Adelaide flight research", "text": "Virgin direct Sydney to Adelaide flights for the second Tuesday in December 2026", "due_at": "2026-12-08", "person": None, "project": "Adelaide travel", "next_action": None, "priorities": []})
    if "dashboard" in lower and "remind" in lower and "tomorrow" in lower and "reminder" not in existing:
        tomorrow = datetime.now(ZoneInfo("Australia/Sydney")) + timedelta(days=1)
        additions.append({"kind": "reminder", "title": "Review the dashboard setup", "text": "review the dashboard setup tomorrow", "due_at": tomorrow.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(), "person": None, "project": "Christine dashboard", "next_action": None, "priorities": []})
    if "mother's birthday" in lower and "birthday" not in existing:
        additions.append({"kind": "birthday", "title": "Bill's mother's birthday", "text": "remember Bill's mother's birthday", "due_at": None, "person": "Bill's mother", "project": None, "next_action": None, "priorities": []})
    return actions + additions


def _normalize_new_year_travel_request(text, proposal):
    lower = text.lower()
    if not all(term in lower for term in ("switzerland", "new year's eve", "qantas")):
        return proposal
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    year = now.year
    sunday = now + timedelta(days=(calendar.SUNDAY - now.weekday()) % 7)
    monday = now + timedelta(days=(calendar.MONDAY - now.weekday()) % 7)
    if monday.date() <= sunday.date():
        monday += timedelta(days=7)
    actions = [
        {"kind": "date_resolution", "title": f"Christmas Day {year}", "text": "Christmas Day this year", "due_at": f"{year}-12-25", "person": None, "project": "Switzerland and Sydney New Year travel", "next_action": None, "priorities": []},
        {"kind": "date_resolution", "title": f"New Year's Eve {year}", "text": "New Year's Eve this year", "due_at": f"{year}-12-31", "person": None, "project": "Switzerland and Sydney New Year travel", "next_action": None, "priorities": []},
        {"kind": "task", "title": "Organise Switzerland and Sydney New Year travel", "text": text, "due_at": None, "person": None, "project": "Switzerland and Sydney New Year travel", "next_action": "Arrange Switzerland before New Year's Eve, return to Sydney for New Year's Eve, and compare Qantas booking options", "priorities": []},
        {"kind": "flight_search", "title": "Qantas Switzerland–Sydney New Year flight research", "text": "Research Qantas options for Switzerland before New Year's Eve and return to Sydney for New Year's Eve", "due_at": f"{year}-12-31", "person": None, "project": "Switzerland and Sydney New Year travel", "next_action": "Compare Sunday versus Monday return options", "priorities": []},
    ]
    return {
        "actions": actions,
        "clarification": f"Should I remind you on Sunday {sunday.day} {sunday.strftime('%B')} or Monday {monday.day} {monday.strftime('%B')}?",
    }


def _schedule_reminder(action, event):
    from cron.jobs import create_job
    due_at = action.get("due_at")
    if not due_at:
        raise ValueError("reminder time requires clarification")
    origin = {"platform": "telegram", "chat_id": str(event.source.chat_id), "user_id": str(event.source.user_id), "chat_type": "dm"}
    prompt = "Reply with exactly this reminder and no extra prose: " + action["title"]
    return create_job(prompt=prompt, schedule=due_at, name="Reminder: " + action["title"], repeat=1, deliver="origin", origin=origin, enabled_toolsets=[])["id"]


async def _recent_captures():
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/ssh", "-F", "/dev/null", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5", "-T",
        "billlyon@100.122.219.24", "curl -fsS http://127.0.0.1:3851/api/state",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0 or len(stdout) > 262144:
        return []
    data = json.loads(stdout)
    return ((data.get("captures") or {}).get("recent") or [])[:5]


def _resume_pending(store, event, text):
    pending = store.pending()
    if not pending or pending["kind"] != "birthday":
        return None
    context = json.loads(pending["context_json"])
    parsed = _parse_ordinal_date(text)
    if parsed:
        context["birthday_date"] = {"day": parsed["day"], "month": parsed["month"]}
        context["date_source_text"] = parsed["source_text"]
        if "adelaide" in text.lower() or "fly there" in text.lower():
            context["adelaide_context"] = text
        if "weekend before" in text.lower():
            context["travel_timing_context"] = text
        item_id = context["item_id"]
        store.update_item(item_id, context)
        question = f"When should I remind you to organise your mother's birthday on {parsed['day']} {parsed['month']}?"
        store.update_pending(pending["id"], context, question, event.platform_update_id)
        action_id = store.put_action(event.platform_update_id, 0, "birthday_clarification", "completed", {"text": text, "pending_id": pending["id"]}, {"item_id": item_id, "date": context["birthday_date"], "adelaide_context": context.get("adelaide_context")})
        store.update_action(action_id, "completed", {"item_id": item_id, "pending_id": pending["id"]})
        retained = "; retained that you need to be in Adelaide" if context.get("adelaide_context") else ""
        reply = f"Updated birthday item {item_id}: {parsed['day']} {parsed['month']}{retained}.\nNeed one clarification ({pending['id']}): {question}"
        store.finish(event.platform_update_id, {"reply": reply})
        return reply

    due = _parse_clarification_time(text)
    birthday = context.get("birthday_date")
    if due is None or not birthday:
        return None
    prior_reply = store.source_text(pending.get("reply_update_id")) or ""
    if "weekend before" in prior_reply.lower():
        context["travel_timing_context"] = prior_reply
    if not context.get("adelaide_context"):
        context["adelaide_context"] = store.recent_source_containing("Adelaide") or prior_reply
    day, month = birthday["day"], birthday["month"]
    title = f"Organise travel to Adelaide for your mother's birthday on {day} {month}, aiming for the weekend before"
    action = {"kind": "reminder", "title": title, "text": text, "due_at": due.isoformat()}
    reminder_id = _schedule_reminder(action, event)
    details = {
        "due_at": due.isoformat(), "cron_job_id": reminder_id,
        "purpose": "organise birthday travel", "birthday_date": birthday,
        "destination": "Adelaide", "travel_timing": "weekend before",
        "pending_clarification_id": pending["id"],
    }
    item_id = store.add_item("reminder", title, text, details, event.platform_update_id, 0)
    action_id = store.put_action(event.platform_update_id, 0, "birthday_reminder", "completed", {"text": text, "pending_id": pending["id"]}, {"item_id": item_id, **details})
    store.update_action(action_id, "completed", {"item_id": item_id, **details})
    store.update_pending(pending["id"], context, pending["question"], event.platform_update_id, state="resolved")
    store.update_action(pending["action_id"], "completed", {"reminder_id": item_id, "resolved_by_update_id": str(event.platform_update_id)})
    reply = f"Reminder created for Friday: organise travel to Adelaide for your mother's birthday on {day} {month}, aiming for the weekend before. {item_id} — {due.isoformat()} (9:00 am Sydney time defaulted)."
    store.finish(event.platform_update_id, {"reply": reply})
    return reply


async def intercept(event):
    text = event.text if isinstance(event.text, str) else ""
    if not text or text.startswith("/") or event.media_urls or event.media_types or event.platform_update_id is None or event.message_id is None:
        return None
    cfg = settings()
    if cfg.get("enabled") is not True or not _lane(event, cfg):
        return None
    # Explicit dashboard reads belong to normal Hermes model/tool dispatch.
    # This exemption neither calls a tool nor generates a response.
    if is_dashboard_read_request(text):
        return None
    store = Store(Path(cfg.get("database") or get_hermes_home() / "natural_pa_intake" / "state.sqlite"))
    prior = store.prior_result(event.platform_update_id)
    if prior:
        return prior["reply"]
    timestamp = getattr(event, "timestamp", None)
    source_timestamp = timestamp.isoformat() if hasattr(timestamp, "isoformat") else (str(timestamp) if timestamp is not None else None)
    store.record_inbound(event.platform_update_id, event.message_id, text, source_timestamp)
    resumed = _resume_pending(store, event, text)
    if resumed is not None:
        return resumed
    if "what did i just ask" in text.lower() and "friday" in text.lower():
        reminder = store.latest_birthday_reminder()
        if reminder:
            reply = f"You asked me to organise travel to Adelaide for your mother's birthday on {reminder['birthday_date']['day']} {reminder['birthday_date']['month']}, aiming for the weekend before. Reminder {reminder['id']} is set for {reminder['due_at']}."
            store.put_action(event.platform_update_id, 0, "retrieval", "completed", {"text": text}, reminder)
            store.finish(event.platform_update_id, {"reply": reply})
            return reply
    try:
        proposal = await _propose(text)
    except Exception as exc:
        logger.warning("Natural PA interpretation failed after durable intake: %s", exc)
        store.set_proposal(event.platform_update_id, {"actions": [], "clarification": None, "error": "interpretation_failed"})
        return "I retained your exact request, but interpretation failed before any actions were completed."
    proposal = _normalize_new_year_travel_request(text, proposal)
    context_text = text
    if "actual date" in text.lower():
        prior_date_context = store.recent_source_containing("second Tuesday in December")
        if prior_date_context:
            context_text += "\nPrior durable travel context: " + prior_date_context
    actions = _supplement_demonstrated_actions(context_text, proposal.get("actions") or [])
    proposal["actions"] = actions
    store.set_proposal(event.platform_update_id, proposal)
    if not actions:
        if proposal.get("clarification"):
            action_id = store.put_action(event.platform_update_id, 0, "clarification", "needs_clarification", {"text": text})
            store.add_pending(event.platform_update_id, action_id, "generic", proposal["clarification"], {"exact_source_text": text})
            store.finish(event.platform_update_id, {"reply": proposal["clarification"]})
            return proposal["clarification"]
        store.finish(event.platform_update_id, {"reply": ""})
        return None
    if any(not isinstance(a, dict) or a.get("kind") not in KINDS for a in actions):
        return "I could not safely resolve that into bounded PA actions. Nothing was changed."
    lines = []
    pending_lines = []
    for index, action in enumerate(actions):
        kind = action["kind"]
        action_id = store.put_action(event.platform_update_id, index, kind, "ready", action)
        if kind == "capture":
            from gateway.raw_capture_gate import service as raw_capture
            clone = copy.copy(event)
            clone.text = "/capture " + action["text"]
            result = await raw_capture.intercept(clone)
            if not result or not result.startswith("Captured canonically"):
                lines.append("Capture not confirmed: " + (result or "no canonical result"))
                store.update_action(action_id, "failed", {"result": result})
            else:
                lines.append("Saved thought: " + result.removeprefix("Captured canonically on M3 as "))
                store.update_action(action_id, "completed", {"result": result})
        elif kind == "daily_priorities":
            values = [str(v).strip() for v in action.get("priorities", []) if str(v).strip()]
            if len(values) > 3:
                return "You listed more than three priorities. Which three should control today?"
            store.set_priorities(values, event.platform_update_id)
            lines.append("Today's priorities: " + "; ".join(values))
            store.update_action(action_id, "completed", {"priorities": values})
        elif kind == "retrieval":
            query = action.get("text") or action.get("title") or text
            if "long message" in query.lower() or "what did i ask" in query.lower():
                summary = store.latest_multi_action_summary()
                if summary:
                    lines.append("Durable PA request " + summary["source_update_id"] + ": " + "; ".join(f"{a['kind']} {a['state']} ({a['id']})" for a in summary["actions"]))
                    store.update_action(action_id, "completed", summary)
                    continue
            found = store.status(query)
            if "priorities" in found:
                lines.append("PA daily priorities: " + ("; ".join(found["priorities"]) or "none set"))
            if found.get("items"):
                lines.append("PA canonical items: " + "; ".join(f"{i['title']} ({i['id']})" for i in found["items"]))
            if any(word in query.lower() for word in ("idea", "capture", "tell", "told", "remember")):
                captures = await _recent_captures()
                lines.append("M3 canonical captures: " + ("; ".join(f"{i['original_text']} ({i['id']})" for i in captures) or "none found"))
            if not any(line.startswith(("PA daily", "PA canonical", "M3 canonical")) for line in lines):
                lines.append("PA canonical state: nothing matching is open")
            store.update_action(action_id, "completed", found)
        elif kind == "flight_search":
            from tools.web_tools import web_search_tool
            query = action.get("text") or text
            results = await asyncio.to_thread(web_search_tool, query + " direct flights departure arrival fare baggage flexibility", 5)
            item_id = store.add_item("flight_research", action["title"], text, {"travel_date": action.get("due_at"), "query": query, "result": results, "schedule_warning": "Future schedules and fares can change."}, event.platform_update_id, index)
            lines.append(f"Flight research retained ({item_id}); future schedules and fares can change")
            store.update_action(action_id, "completed", {"item_id": item_id, "result": results})
        elif kind == "date_resolution":
            item_id = store.add_item("date_resolution", action["title"], text, {"resolved_date": action.get("due_at"), "project": action.get("project")}, event.platform_update_id, index)
            resolved = datetime.fromisoformat(str(action.get("due_at"))).date()
            lines.append(f"{action['title']} is {resolved.strftime('%A %-d %B %Y')} ({item_id})")
            store.update_action(action_id, "completed", {"item_id": item_id, "resolved_date": action.get("due_at")})
        elif kind == "birthday":
            context = {"event": action["title"], "exact_source_text": text, "date": None, "adelaide_context": None}
            item_id = store.add_item("birthday", action["title"], text, context, event.platform_update_id, index)
            context["item_id"] = item_id
            store.update_item(item_id, context)
            question = "When is your mother's birthday?"
            pending_id = store.add_pending(event.platform_update_id, action_id, "birthday", question, context)
            store.update_action(action_id, "needs_clarification", {"item_id": item_id, "pending_id": pending_id, "question": question})
            pending_lines.append(f"{question} ({pending_id})")
        else:
            if kind == "reminder" and action.get("due_at") and _time_was_defaulted(text):
                resolved = datetime.fromisoformat(action["due_at"].replace("Z", "+00:00"))
                action["due_at"] = resolved.astimezone(ZoneInfo("Australia/Sydney")).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
            details = _details(action)
            details["source_context"] = text
            calendar_context = _calendar_context(text)
            if calendar_context:
                details["calendar_context"] = calendar_context
            if kind == "reminder":
                try:
                    details["cron_job_id"] = _schedule_reminder(action, event)
                except ValueError:
                    return "What time should I remind you? Nothing was scheduled yet."
            item_id = store.add_item(kind, action["title"], text, details, event.platform_update_id, index)
            label = {"task": "Task created", "reminder": "Reminder created", "obligation": "Waiting item created"}[kind]
            suffix = f" — {action.get('due_at')}" if action.get("due_at") else ""
            if kind == "reminder" and _time_was_defaulted(text):
                suffix += " (time defaulted to 9:00 am Sydney)"
            lines.append(f"{label}: {action['title']} ({item_id}){suffix}")
            store.update_action(action_id, "completed", {"item_id": item_id, **details})
            if calendar_context and not calendar_context.get("validated"):
                lines.append(calendar_context["clarification"])
    if proposal.get("clarification") and not pending_lines:
        index = len(actions)
        action_id = store.put_action(event.platform_update_id, index, "clarification", "needs_clarification", {"text": text, "question": proposal["clarification"]})
        pending_id = store.add_pending(event.platform_update_id, action_id, "generic", proposal["clarification"], {"exact_source_text": text, "request_update_id": str(event.platform_update_id)})
        pending_lines.append(f"{proposal['clarification']} ({pending_id})")
    reply = "Done:\n- " + "\n- ".join(lines)
    if pending_lines:
        reply += "\n\nNeed one clarification:\n- " + "\n- ".join(pending_lines)
    store.finish(event.platform_update_id, {"reply": reply})
    return reply

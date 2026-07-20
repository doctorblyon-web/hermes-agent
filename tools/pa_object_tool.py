"""Model-callable ``pa_object`` tool — Christine's bounded canonical PA write.

Christine's model owns the conversation. It reads Bill's ordinary speech,
interprets likely intent, asks natural follow-up questions across as many turns
as it needs, and ONLY THEN — when meaning is sufficiently clear — calls this tool
to record or transition a canonical PA object (an obligation Bill owes / is
waiting on, or a ``needs_bill`` decision only Bill can make).

Everything consequential lives behind the tool, not before the model:

* the tool never interprets natural language — it takes structured arguments the
  model has already decided on;
* the write is M3-first and success is claimed only on a verified canonical
  receipt (``ok: true``); nothing is ever "done" until then;
* a repeated identical call in the same session is idempotent (no duplicate);
* there is no delete path — objects only reach a terminal status.

The actual pipeline is :func:`gateway.pa_objects.service.tool_create` /
:func:`~gateway.pa_objects.service.tool_transition`; this module only exposes them
to the model as a single registered tool and passes the session id through so the
idempotency key is stable across a replayed tool call within one conversation.
"""

from __future__ import annotations

import logging

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _gate_enabled() -> bool:
    """The tool schema is offered to the model only when the bounded PA-object
    substrate is enabled for this deployment."""
    try:
        from gateway.pa_objects import service
        return service.settings().get("enabled") is True
    except Exception:
        return False


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================
# Behavioural guidance is baked into the description so it is part of the static,
# cached tool schema. The rules constrain WHEN the model writes, not whether it
# may reason — reasoning and conversation happen freely, without this tool.

PA_OBJECT_SCHEMA = {
    "name": "pa_object",
    "description": (
        "Record or update one of Bill's canonical personal-assistant items. The "
        "write is the LAST step of understanding a request, never the first.\n\n"
        "Two kinds of item:\n"
        "- obligation: something Bill has to do, owes, or is waiting on.\n"
        "- needs_bill: a decision only Bill can make.\n\n"
        "CLARIFY BEFORE YOU WRITE (most important rule):\n"
        "- Calling this tool is the final step, taken only once the request is "
        "sufficiently complete OR Bill has confirmed. Do NOT record first and ask "
        "questions afterwards — that is the exact failure this rule exists to "
        "prevent.\n"
        "- If any material detail is missing or ambiguous (e.g. which event/match, "
        "how many, who, by when, which existing item), DO NOT call the tool this "
        "turn. Ask Bill one natural follow-up question and wait for his answer. "
        "An incomplete request is a conversation, not a write.\n"
        "  Example — Bill: 'I need to organise tickets for the Swans.' This is "
        "incomplete: which match, how many, any budget? Ask first; write nothing "
        "yet. Only once he has answered do you record one obligation.\n"
        "- A clear, complete obligation needs no interrogation: 'Send Gina the "
        "revised material by Friday' can be recorded directly (confirm first only "
        "for consequential changes — see below). Clarify when details are "
        "genuinely missing, not to stall.\n\n"
        "ACCOUNT FOR EVERY ITEM (no silent omission):\n"
        "- When Bill's message contains several items, reflect back ALL of them "
        "before writing, so your understanding is checkable against what he said. "
        "Never drop one silently.\n"
        "- If a lane has a cap or an item doesn't fit it (e.g. Goals hold at most "
        "three; a fourth 'goal' won't fit), say so explicitly and propose where "
        "the remainder should go (e.g. record it as an obligation) — then let Bill "
        "decide. Do not quietly discard the overflow.\n\n"
        "DATES — never silently redate:\n"
        "- Use the day Bill actually said. 'tomorrow' means tomorrow (Australia/"
        "Sydney), not today; a named day means that day. If the day is unclear, "
        "ask — never assume today.\n"
        "- Never invent a date, person, project, option, or decision. Pass a field "
        "only if Bill actually gave it; otherwise leave it out.\n\n"
        "OTHER RULES:\n"
        "- Preserve Bill's own wording. Put the verbatim substring he used in "
        "'wording' / 'decision' — do not paraphrase, tidy, or expand it.\n"
        "- Do not create a second item for something already recorded this "
        "conversation; the tool is idempotent but you should not try to "
        "duplicate.\n\n"
        "OPERATIONS:\n"
        "- operation='create': make a new obligation or needs_bill. Required: "
        "kind, wording. Optional for an obligation: person, due, project.\n"
        "- operation='transition': change an existing item's status. Required: "
        "kind, action, and either reference (a few words from the item to find "
        "it) or object_id. For action='resolve' also give decision (verbatim).\n"
        "  obligation actions: set_waiting, resume, complete, cancel.\n"
        "  needs_bill actions: resolve, cancel.\n\n"
        "'due' accepts an explicit date (YYYY-MM-DD) or a natural phrase Bill "
        "used ('Friday', 'tomorrow night'); it is resolved in Australia/Sydney "
        "and never guessed — omit it if Bill gave no due date.\n\n"
        "RESULT: JSON. 'ok':true with a verified canonical receipt means the item "
        "is genuinely recorded — only then may you tell Bill it is done, using the "
        "returned 'message' (rephrase naturally if you like). 'ok':false means "
        "NOTHING changed — report that honestly and, if the tool asked for a "
        "clarification (e.g. more than one item matched), pass it on to Bill. "
        "Consequential changes (completing/cancelling/resolving an item) should "
        "be confirmed with Bill before you call the tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create", "transition"],
                "description": "'create' a new item, or 'transition' an existing one.",
            },
            "kind": {
                "type": "string",
                "enum": ["obligation", "needs_bill"],
                "description": "obligation = something to do / owe / wait on; needs_bill = a decision only Bill can make.",
            },
            "wording": {
                "type": "string",
                "description": "create only. Bill's own verbatim wording for the item. Never paraphrased.",
            },
            "person": {
                "type": "string",
                "description": "create (obligation) only. A person the obligation is directed at, if Bill named one. Omit otherwise.",
            },
            "due": {
                "type": "string",
                "description": "create (obligation) only. A due date Bill actually gave: YYYY-MM-DD or a natural phrase ('Friday'). Australia/Sydney. Omit if none.",
            },
            "project": {
                "type": "string",
                "description": "create (obligation) only. An explicit project Bill named. Omit otherwise.",
            },
            "action": {
                "type": "string",
                "enum": ["set_waiting", "resume", "complete", "cancel", "resolve"],
                "description": "transition only. obligation: set_waiting|resume|complete|cancel. needs_bill: resolve|cancel.",
            },
            "reference": {
                "type": "string",
                "description": "transition only. A few words from the target item (or the person) so the right one is found. Use this or object_id.",
            },
            "object_id": {
                "type": "string",
                "description": "transition only. The exact object id, if known. Otherwise use reference.",
            },
            "decision": {
                "type": "string",
                "description": "transition + action='resolve' only. Bill's verbatim decision.",
            },
        },
        "required": ["operation"],
    },
}


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


async def pa_object_handler(args: dict, **kw) -> str:
    """Route a model tool call to the bounded M3-first PA-object write."""
    from gateway.pa_objects import service

    session_id = kw.get("session_id")
    operation = (args.get("operation") or "").strip().lower()
    kind = (args.get("kind") or "").strip().lower() or None

    if operation == "create":
        if kind not in ("obligation", "needs_bill"):
            return tool_error("create needs kind='obligation' or 'needs_bill'.", ok=False)
        if _blank(args.get("wording")):
            return tool_error("create needs Bill's verbatim wording.", ok=False)
        result = await service.tool_create(
            kind,
            args.get("wording"),
            person=args.get("person"),
            due=args.get("due"),
            project=args.get("project"),
            session_id=session_id,
        )
        return tool_result(result)

    if operation == "transition":
        if kind not in ("obligation", "needs_bill"):
            return tool_error("transition needs kind='obligation' or 'needs_bill'.", ok=False)
        action = (args.get("action") or "").strip().lower() or None
        if not action:
            return tool_error("transition needs an action.", ok=False)
        if _blank(args.get("reference")) and _blank(args.get("object_id")):
            return tool_error(
                "transition needs a reference (a few words from the item) or an object_id.",
                ok=False,
            )
        result = await service.tool_transition(
            kind,
            action,
            reference=args.get("reference"),
            object_id=args.get("object_id"),
            decision=args.get("decision"),
            session_id=session_id,
        )
        return tool_result(result)

    return tool_error("operation must be 'create' or 'transition'.", ok=False)


registry.register(
    name="pa_object",
    toolset="pa_object",
    schema=PA_OBJECT_SCHEMA,
    handler=pa_object_handler,
    check_fn=_gate_enabled,
    is_async=True,
    emoji="🗂️",
)

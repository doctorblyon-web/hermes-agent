import logging

from gateway.raw_capture_gate import service as raw_capture
from gateway.thoughts_gate import schema

logger = logging.getLogger(__name__)

SAVED_REPLY = "Saved as a thought."
EMPTY_REPLY = "I didn’t catch the thought itself, so nothing was saved."
# Covers rejected, indeterminate and malformed canonical outcomes alike: none
# of them may ever read as success. Deliberately free of JSON, IDs, hashes and
# receipt internals.
UNVERIFIED_REPLY = (
    "I couldn’t confirm that thought was saved canonically, so I’m not claiming it was."
)


def _platform(source):
    return getattr(
        getattr(source, "platform", None), "value", getattr(source, "platform", None)
    )


def _in_bill_lane(event):
    """Thoughts ride the Raw Capture lane, so they honour its exact bounds."""
    try:
        cfg = raw_capture.settings()
    except Exception as exc:
        logger.error("Thought capture configuration unavailable: %s", exc)
        return False
    source = getattr(event, "source", None)
    return (
        cfg.get("enabled") is True
        and _platform(source) == "telegram"
        and getattr(source, "chat_type", None) == "dm"
        and str(getattr(source, "user_id", None)) == str(cfg.get("bill_user_id"))
        and str(getattr(source, "chat_id", None)) == str(cfg.get("chat_id"))
    )


async def intercept(event):
    """Return None for baseline routing, or a plain thought acknowledgement."""
    thought = schema.parse(getattr(event, "text", None))
    if thought is None:
        # Ordinary conversation always keeps its baseline route.
        return None
    if not _in_bill_lane(event):
        return None
    if not thought:
        return EMPTY_REPLY
    result = await raw_capture.capture_result(event, thought, object_type="thought")
    if result is None or result.object_type != "thought":
        # No verified typed receipt: say nothing that implies a canonical save.
        return UNVERIFIED_REPLY
    return SAVED_REPLY

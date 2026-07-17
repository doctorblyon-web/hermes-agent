import re

# Only clear, explicit thought-capture instructions are recognised, and only
# at the very start of the message. Anchoring is what keeps ordinary
# conversation that merely contains an opinion or an idea from being captured:
# "I think the dashboard should show attention" states a view and is left
# alone; "Save this as a thought: ..." issues an instruction.
_INSTRUCTION = (
    r"(?:"
    r"don[’']?t\s+let\s+me\s+lose\s+this\s+thought"
    r"|save\s+this\s+as\s+a\s+thought"
    r"|remember\s+this\s+idea\s+as\s+a\s+thought"
    r"|capture\s+this\s+thought"
    r")"
)
# The instruction is separated from the thought by a dash, colon or comma.
# Requiring a separator avoids swallowing a sentence that merely opens with
# similar words but never hands over a thought.
_SEPARATOR = r"[:,—–-]"
_THOUGHT_REQUEST = re.compile(
    rf"\A\s*{_INSTRUCTION}\s*{_SEPARATOR}\s*(?P<thought>[\s\S]*)\Z",
    re.IGNORECASE,
)


def parse(text: str | None) -> str | None:
    """Return None when this is not an explicit thought request.

    Otherwise return the intended thought text exactly as Bill wrote it, with
    only the instruction, the separator and the whitespace around it removed.
    The returned text is never rewritten, normalised or prefixed; an empty
    string means the instruction carried no thought.
    """
    if not isinstance(text, str):
        return None
    match = _THOUGHT_REQUEST.match(text)
    if not match:
        return None
    return match.group("thought").strip()


def is_thought_request(text: str | None) -> bool:
    """Identify explicit thought syntax without loading any configuration."""
    return parse(text) is not None

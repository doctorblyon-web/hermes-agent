import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

MISSIONS = frozenset({"billos", "signalpath", "legal", "knowledgebrains", "investment"})
ENVELOPE = re.compile(
    r"\ACaptured\.\n```signal\n(?P<payload>.+?)\n```\n"
    r"(?:(?P<replacement>Today’s goals are already set\. Reply (?:Y|\*\*Y\*\*) within 30 minutes to replace them, or ignore this proposal to keep them\.)|"
    r"(?P<today_goals>Captured as today’s goals proposal\. Reply (?:Y|\*\*Y\*\*) within 30 minutes to approve canonical application\.)|"
    r"Captured as a proposal\. Reply (?:Y|\*\*Y\*\*) within 30 minutes to approve canonical application\.)\Z",
    re.DOTALL,
)
TODAY_GOALS_ENVELOPE = re.compile(
    r"\AI’ve prepared these as today’s goals:\n\n(?P<goals>.+)\n\n"
    r"(?:(?P<replacement>Today’s goals are already set\. Reply Y within 30 minutes to replace them, or ignore this proposal to keep them\.)|"
    r"(?P<today_goals>Reply Y within 30 minutes to set them\.))\Z",
    re.DOTALL,
)


class ValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Proposal:
    actions: tuple
    no_today: None = None
    replacement: bool = False
    today_goals: bool = False

    def as_dict(self):
        return {"actions": list(self.actions), "no_today": None}


def _text(value, index):
    if not isinstance(value, str):
        raise ValidationError("wrong_type", f"actions[{index}].text")
    if value != value.strip() or not value or len(value) > 200:
        raise ValidationError("invalid_length_or_trim", f"actions[{index}].text")
    if any(c in value for c in "|\r\n`"):
        raise ValidationError("forbidden_character", f"actions[{index}].text")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in value):
        raise ValidationError("control_character", f"actions[{index}].text")
    if value.startswith("#") or re.match(r"(?i)^NOT\s+TODAY\s*:", value):
        raise ValidationError("forbidden_prefix", f"actions[{index}].text")
    return value


def parse_display(text: str):
    goals_match = TODAY_GOALS_ENVELOPE.match(text or "")
    if goals_match:
        lines = goals_match.group("goals").splitlines()
        if not 1 <= len(lines) <= 3:
            raise ValidationError("action_count")
        actions = []
        for i, line in enumerate(lines, 1):
            match = re.fullmatch(rf"{i}\. (.*)", line)
            if not match:
                raise ValidationError("goal_numbering", str(i))
            actions.append({"text": _text(match.group(1), i - 1), "mission": "billos"})
        return Proposal(
            tuple(actions),
            replacement=goals_match.group("replacement") is not None,
            today_goals=True,
        )
    match = ENVELOPE.match(text or "")
    if not match:
        return None
    try:
        raw = json.loads(match.group("payload"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid_json", str(exc)) from exc
    if not isinstance(raw, dict) or set(raw) != {"actions", "no_today"}:
        raise ValidationError("top_level_keys")
    if raw["no_today"] is not None:
        raise ValidationError("no_today_must_be_null")
    if not isinstance(raw["actions"], list) or len(raw["actions"]) != 3:
        raise ValidationError("action_count")
    actions = []
    for i, item in enumerate(raw["actions"]):
        if not isinstance(item, dict) or set(item) != {"text", "mission"}:
            raise ValidationError("action_keys", str(i))
        mission = item["mission"]
        if mission not in MISSIONS:
            raise ValidationError("mission_not_permitted", str(i))
        actions.append({"text": _text(item["text"], i), "mission": mission})
    return Proposal(
        tuple(actions),
        replacement=match.group("replacement") is not None,
        today_goals=(
            match.group("replacement") is not None
            or match.group("today_goals") is not None
        ),
    )


def canonical_json(proposal: Proposal) -> str:
    return json.dumps(proposal.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_display(proposal: Proposal) -> str:
    if proposal.today_goals:
        goals = "\n".join(
            f"{index}. {action['text']}"
            for index, action in enumerate(proposal.actions, 1)
        )
        instruction = (
            "Today’s goals are already set. Reply Y within 30 minutes to replace them, "
            "or ignore this proposal to keep them."
            if proposal.replacement else
            "Reply Y within 30 minutes to set them."
        )
        return f"I’ve prepared these as today’s goals:\n\n{goals}\n\n{instruction}"
    payload = json.dumps(proposal.as_dict(), ensure_ascii=False, separators=(",", ":"))
    instruction = (
        "Today’s goals are already set. Reply Y within 30 minutes to replace them, "
        "or ignore this proposal to keep them."
        if proposal.replacement else
        "Captured as today’s goals proposal. Reply Y within 30 minutes to approve canonical application."
        if proposal.today_goals else
        "Captured as a proposal. Reply Y within 30 minutes to approve canonical application."
    )
    return (
        "Captured.\n```signal\n"
        f"{payload}\n```\n"
        f"{instruction}"
    )


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

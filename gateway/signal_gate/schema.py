import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

MISSIONS = frozenset({"billos", "signalpath", "legal", "knowledgebrains", "investment"})
ENVELOPE = re.compile(
    r"\ACaptured\.\n```signal\n(?P<payload>.+?)\n```\n"
    r"Captured as a proposal\. Reply Y within 30 minutes to approve canonical application\.\Z",
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
    return Proposal(tuple(actions))


def canonical_json(proposal: Proposal) -> str:
    return json.dumps(proposal.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

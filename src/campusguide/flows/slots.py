"""槽位填充、校验与更正（确定性规则）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from campusguide.domain.models import SlotDefinition, SlotValue

_DECLINE_RE = re.compile(r"^(不想说|不想回答|跳过|不知道|不确定|不清楚|随便|不填)[。！!？?\s]*$")
_GRADE_CN = {
    "大一": 1,
    "大二": 2,
    "大三": 3,
    "大四": 4,
    "大五": 5,
    "研一": 1,
    "研二": 2,
    "研三": 3,
}
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?!\d)")
_BOOL_POSITIVE = re.compile(r"(是的|是|有过|受过|有处分|有|已认定|认定过|拿到|获得|已获|取得)")
_BOOL_NEGATIVE = re.compile(
    r"(不是|否|没有|没受过|未受过|无处分|无|未认定|没认定|没拿到|未获得|没获得|未取得)"
)


@dataclass
class SlotExtraction:
    values: dict[str, SlotValue] = field(default_factory=dict)
    declined_slots: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.values or self.declined_slots)


def _normalize(text: str) -> str:
    return text.strip().lower()


def _extract_integer(definition: SlotDefinition, text: str, pending: bool) -> int | None:
    normalized = _normalize(text)
    match = re.search(r"大[一二三四五]", normalized)
    if match:
        value = _GRADE_CN.get(match.group(0))
    else:
        match = re.search(r"研[一二三]", normalized)
        if match:
            value = _GRADE_CN.get(match.group(0))
        else:
            match = re.search(r"([1-5])\s*年级", normalized)
            if match:
                value = int(match.group(1))
            elif pending and re.fullmatch(r"[1-5]", normalized):
                value = int(normalized)
            else:
                value = None
    if value is None:
        return None
    validation = definition.validation or {}
    minimum = validation.get("min")
    maximum = validation.get("max")
    if minimum is not None and value < int(minimum):
        return None
    if maximum is not None and value > int(maximum):
        return None
    return value


_BOOLEAN_CONTEXT = {
    "has_record": ("处分",),
    "has_financial_aid": ("认定", "困难", "贫困"),
    "is_graduating": ("应届", "毕业", "大四"),
    "has_recommendation_quota": ("名额", "推免资格", "推荐资格"),
    "in_recommendation_list": ("名单", "资格", "公示"),
}


def _extract_boolean(definition: SlotDefinition, text: str, pending: bool) -> bool | None:
    normalized = _normalize(text)
    if "是否" in normalized:
        return None
    context = _BOOLEAN_CONTEXT.get(definition.name, ())
    clauses = re.split(r"[，,。；;、\n]+", normalized)
    relevant = [clause for clause in clauses if any(word in clause for word in context)]
    if relevant:
        probe = " ".join(relevant)
    elif pending or not context:
        probe = normalized
    else:
        return None
    if definition.name == "has_financial_aid":
        if re.search(r"(不是|没有|未|没).{0,4}(困难|认定|贫困)", probe):
            return False
        if re.search(r"(困难|认定|贫困)", probe) and _BOOL_NEGATIVE.search(probe) is None:
            return True
        return None
    if definition.name == "is_graduating":
        if re.search(r"(不是|非|不).{0,3}(应届|毕业)", probe):
            return False
        if re.search(r"(应届|毕业|大四)", probe) and _BOOL_NEGATIVE.search(probe) is None:
            return True
    if definition.name == "in_recommendation_list" and "名单" in probe:
        return not re.search(r"(不在|未在|没在|没有在).{0,3}名单", probe)
    if _BOOL_NEGATIVE.search(probe):
        return False
    if _BOOL_POSITIVE.search(probe):
        return True
    if pending and probe in {"不", "没"}:
        return False
    return None


def _extract_enum(definition: SlotDefinition, text: str) -> str | None:
    normalized = _normalize(text)
    candidates: list[tuple[int, str]] = []
    for canonical in definition.allowed_values or []:
        aliases = [canonical, *(definition.aliases or {}).get(canonical, [])]
        for alias in aliases:
            alias_normalized = _normalize(alias)
            if alias_normalized and alias_normalized in normalized:
                candidates.append((len(alias_normalized), canonical))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _extract_date(definition: SlotDefinition, text: str) -> date | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _extract_string(definition: SlotDefinition, text: str, pending: bool) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if pending and len(stripped) <= 120:
        return stripped
    return None


def extract_slots(
    message: str,
    definitions: list[SlotDefinition],
    *,
    turn_id: int,
    existing: dict[str, SlotValue] | None = None,
    pending_slot: str | None = None,
    allow_overwrite: bool = True,
) -> SlotExtraction:
    """从一条用户消息中抽取明确槽位。

    只填“明确表达”的值；不确定就不填。pending_slot 用于消解布尔/纯数字歧义。
    """
    existing = existing or {}
    result = SlotExtraction()
    normalized = _normalize(message)
    if pending_slot and _DECLINE_RE.match(normalized):
        result.declined_slots.append(pending_slot)
        return result

    ordered = sorted(
        definitions,
        key=lambda item: (0 if item.name == pending_slot else 1, item.priority, item.name),
    )
    for definition in ordered:
        current = existing.get(definition.name)
        if current is not None and not allow_overwrite:
            continue
        pending = definition.name == pending_slot
        value = None
        if definition.type == "integer":
            value = _extract_integer(definition, message, pending)
        elif definition.type == "boolean":
            value = _extract_boolean(definition, message, pending)
        elif definition.type == "enum":
            value = _extract_enum(definition, message)
        elif definition.type == "date":
            value = _extract_date(definition, message)
        elif definition.type == "string":
            value = _extract_string(definition, message, pending)
        if value is None:
            continue
        if current is not None and getattr(current, "value", None) == value:
            continue
        result.values[definition.name] = SlotValue(
            name=definition.name,
            value=value,
            source="user_explicit",
            turn_id=turn_id,
        )
    return result


def validate_slot_value(definition: SlotDefinition, value: object) -> bool:
    if definition.type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        validation = definition.validation or {}
        if validation.get("min") is not None and value < int(validation["min"]):
            return False
        return not (validation.get("max") is not None and value > int(validation["max"]))
    if definition.type == "boolean":
        return isinstance(value, bool)
    if definition.type == "enum":
        return value in (definition.allowed_values or [])
    if definition.type == "date":
        return isinstance(value, date)
    return isinstance(value, str)

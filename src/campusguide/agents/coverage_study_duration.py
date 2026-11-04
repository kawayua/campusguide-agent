"""Scoped study-duration fields and final-answer binding."""

from __future__ import annotations

import re
from dataclasses import dataclass

ORDINARY_MAX = "ordinary_max"
STARTUP_MAX = "startup_max"
BASIC = "basic"
MAXIMUM = "maximum"

_YEAR_SYSTEM = re.compile(
    r"(?<![\d零〇一二两三四五六七八九十百千万])"
    r"([0-9零〇一二两三四五六七八九十百]{1,4})年制"
)
_MODE = re.compile(r"非全日制|全日制")
_BASIC_CUE = re.compile(r"基本修业年限|基本学制|学制")
_MAX_CUE = re.compile(r"最长学习年限|最长修业年限|最长年限|最长")
_ORDINARY_CUE = re.compile(r"最长|最多|上限")
_STARTUP_CUE = re.compile(r"创业")
_DURATION_MEASURE = re.compile(
    r"学习年限|修业年限|培养年限|修读年限|最长年限|年限|学制"
)
_OTHER_MEASURE = re.compile(
    r"学费|住宿费|教材费|报名费|考试费|杂费|费用|收费|金额|数额|"
    r"学分|学时|人数|名额|门数|课程数|分数|成绩|绩点|年龄"
)
_MEASURE_NOUN = re.compile(
    rf"(?:{_DURATION_MEASURE.pattern})|(?:{_OTHER_MEASURE.pattern})"
)
_DURATION_ASK_MARK = re.compile(r"几\s*年|多少\s*年|多久|多长|多长时间")
_OTHER_MEASURE_AFTER = re.compile(
    r"(?:多少|几)\s*(?:人|名|位|门|个|元|份|页|天|周|次|小时|分钟)"
)
_CLAUSE_BREAK = re.compile(r"[，,；;。：:\n？?！!]")
_UNCERTAIN = re.compile(
    r"尚不明确|不明确|不确定|未明确|未知|待定|暂未|未公布|未提及|[?？]|吗|是否"
)
_SHARED = re.compile(r"均|都|统一")
_QUOTED = re.compile(
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'"
)
_DURATION = re.compile(
    r"(?<![\d零〇一二两三四五六七八九十百千万第])"
    r"(\d{1,3}|[零〇一二两三四五六七八九十百]{1,5})\s*年(?!制|级|度)"
)
_CONDITION = re.compile(
    r"(?<![\d零〇一二两三四五六七八九十百千万])"
    r"(?:[0-9]{1,4}|[零〇一二两三四五六七八九十百]{1,5})年制"
)
_BREAK = re.compile(r"[，,；;。：:\n]")
_STRONG_BREAKS = "；;。\n"
_ASK_BREAK = re.compile(r"[，,；;。\n?？!！]")
_ASK_INTENT = re.compile(
    r"几\s*年|多少\s*年|多久|多长|多长时间|"
    r"(?:分别|各|都|均|统一)?\s*(?:是|为|有)?\s*多少|"
    r"对比|比较|区别|差异|哪个更?长|哪一个?更?长|哪边长|谁更长|长短|"
    r"有什么不同|有何不同|不同点|不同之处"
)
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2,
    "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9,
}


@dataclass(frozen=True)
class StudyDurationField:
    condition: str
    kind: str
    cue: str


def _to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if "十" not in text and "百" not in text:
        digits = []
        for char in text:
            if char not in _CN_DIGITS:
                return None
            digits.append(str(_CN_DIGITS[char]))
        return int("".join(digits)) if digits else None
    total = current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def _valid_number(value: int | None) -> bool:
    return value is not None and value > 0 and not 1900 <= value <= 2100


def _duration_at(text: str, start: int = 0) -> int | None:
    for match in _DURATION.finditer(text, start):
        value = _to_int(match.group(1))
        if _valid_number(value):
            return value
    return None


def _blank_quotes(text: str) -> str:
    return _QUOTED.sub(lambda match: " " * len(match.group(0)), text)


def _is_uncertain(text: str) -> bool:
    return bool(_UNCERTAIN.search(text))


def _paren_spans(text: str) -> list[tuple[int, int]]:
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(text):
        if char in "（(":
            stack.append(index)
        elif char in "）)" and stack:
            spans.append((stack.pop(), index + 1))
    return spans


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _conditions(text: str) -> list[tuple[int, str]]:
    spans = _paren_spans(text)
    found: list[tuple[int, str]] = []
    for match in _CONDITION.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        value = _to_int(match.group(0)[:-2])
        if _valid_number(value):
            found.append((match.start(), match.group(0)))
    return found


def _mode_names(text: str) -> list[str]:
    names: list[str] = []
    for match in _MODE.finditer(text):
        name = match.group(0)
        if name not in names:
            names.append(name)
    return names


def _unique_conditions(question: str) -> list[str]:
    conditions: list[str] = []
    seen: set[int] = set()
    for match in _YEAR_SYSTEM.finditer(question):
        value = _to_int(match.group(1))
        if not _valid_number(value) or value in seen:
            continue
        seen.add(value)
        conditions.append(match.group(0))
    return conditions


def _clause_before(text: str, position: int) -> str:
    start = 0
    for match in _CLAUSE_BREAK.finditer(text, 0, position):
        start = match.end()
    return text[start:position]


def _clause_after(text: str, position: int) -> str:
    match = _CLAUSE_BREAK.search(text, position)
    return text[position:match.start()] if match else text[position:]


def _last_measure(text: str) -> str | None:
    found: str | None = None
    for match in _MEASURE_NOUN.finditer(text):
        found = match.group(0)
    return found


def _first_measure(text: str) -> str | None:
    match = _MEASURE_NOUN.search(text)
    return match.group(0) if match else None


def _is_duration_measure(measure: str) -> bool:
    return bool(_DURATION_MEASURE.fullmatch(measure))


def _bound_cue_is_duration(
    question: str, cue: str, start: int, end: int
) -> bool:
    after = _clause_after(question, end)
    if cue == "上限":
        measure = _first_measure(after)
        if measure is not None:
            return _is_duration_measure(measure)
        if _OTHER_MEASURE_AFTER.search(after):
            return False
        before = _last_measure(_clause_before(question, start))
        if before is not None:
            return _is_duration_measure(before)
        return bool(_DURATION_ASK_MARK.search(after))
    measure = _first_measure(after)
    if measure is not None:
        return _is_duration_measure(measure)
    if _OTHER_MEASURE_AFTER.search(after):
        return False
    if _DURATION_ASK_MARK.search(after):
        return True
    before = _last_measure(_clause_before(question, start))
    return before is None or _is_duration_measure(before)


def _cue_asks_duration(
    question: str, cue: str, start: int, end: int
) -> bool:
    if not _bound_cue_is_duration(question, cue, start, end):
        return False
    rest = question[end:]
    stop = _ASK_BREAK.search(rest)
    segment = rest[:stop.start()] if stop else rest
    return bool(_ASK_INTENT.search(segment))


def _asks_year_system(question: str) -> tuple[bool, bool]:
    startup_match = _STARTUP_CUE.search(question)
    pre_startup = question[:startup_match.start()] if startup_match else question
    ordinary = any(
        _cue_asks_duration(pre_startup, match.group(0), match.start(), match.end())
        for match in _ORDINARY_CUE.finditer(pre_startup)
    )
    if startup_match and not ordinary:
        for match in re.finditer(
            r"(?:通常|普通|一般|正常)(?:(?!创业|休学).){0,8}(?P<cue>最长|最多|上限)",
            question,
        ):
            if _cue_asks_duration(
                question,
                match.group("cue"),
                match.start("cue"),
                match.end("cue"),
            ):
                ordinary = True
                break
    return ordinary, bool(startup_match)


def _asks_attribute_intent(question: str, cue: re.Pattern[str]) -> bool:
    for match in cue.finditer(question):
        rest = question[match.end():]
        stop = _ASK_BREAK.search(rest)
        segment = rest[:stop.start()] if stop else rest
        if _ASK_INTENT.search(segment):
            return True
    return False


def study_duration_fields(question: str) -> list[StudyDurationField]:
    if not isinstance(question, str) or not question.strip():
        return []
    modes = _mode_names(question)
    if modes:
        wants_basic = _asks_attribute_intent(question, _BASIC_CUE)
        wants_max = _asks_attribute_intent(question, _MAX_CUE)
        fields: list[StudyDurationField] = []
        for mode in modes:
            if wants_basic:
                fields.append(StudyDurationField(mode, BASIC, f"{mode}学制"))
            if wants_max:
                fields.append(
                    StudyDurationField(mode, MAXIMUM, f"{mode}最长学习年限")
                )
        return fields
    wants_ordinary, wants_startup = _asks_year_system(question)
    if wants_startup and not _asks_attribute_intent(question, _STARTUP_CUE):
        wants_startup = False
    if not wants_ordinary and not wants_startup:
        return []
    fields = []
    for condition in _unique_conditions(question):
        if wants_ordinary:
            fields.append(
                StudyDurationField(
                    condition, ORDINARY_MAX, f"{condition}最长学习年限"
                )
            )
        if wants_startup:
            fields.append(
                StudyDurationField(
                    condition, STARTUP_MAX, f"{condition}创业最长学习年限"
                )
            )
    return fields


def _value_is_startup(segment: str, position: int) -> bool:
    before = segment[:position]
    startup_matches = list(_STARTUP_CUE.finditer(before))
    ordinary_matches = list(_ORDINARY_CUE.finditer(before))
    if not startup_matches:
        return False
    if not ordinary_matches or startup_matches[-1].start() > ordinary_matches[-1].start():
        return True
    gap = before[startup_matches[-1].end():ordinary_matches[-1].start()]
    return _duration_at(gap) is None


def _segment_has_ordinary_topic(segment: str) -> bool:
    if _is_uncertain(segment):
        return False
    startup_matches = list(_STARTUP_CUE.finditer(segment))
    for ordinary_match in _ORDINARY_CUE.finditer(segment):
        previous = [match for match in startup_matches if match.end() <= ordinary_match.start()]
        if not previous:
            return True
        gap = segment[previous[-1].end():ordinary_match.start()]
        if _duration_at(gap) is not None:
            return True
    return False


def _branch_values(
    branch: str, base: int, parens: list[tuple[int, int]]
) -> tuple[list[int], list[int], bool]:
    local = [
        (start - base, end - base)
        for start, end in parens
        if base <= start and end <= base + len(branch)
    ]
    outside = _mask(branch, local)
    if re.search(r"[?？]|吗", outside):
        return [], [], False
    startup: list[int] = []
    for start, end in local:
        content = branch[start + 1:end - 1]
        if _is_uncertain(content):
            continue
        if _STARTUP_CUE.search(content):
            value = _duration_at(content)
            if value is not None:
                startup.append(value)
    ordinary: list[int] = []
    has_ordinary_cue = False
    for segment in _BREAK.split(outside):
        if _is_uncertain(segment):
            continue
        if _segment_has_ordinary_topic(segment):
            has_ordinary_cue = True
        for match in _DURATION.finditer(segment):
            value = _to_int(match.group(1))
            if not _valid_number(value):
                continue
            if _value_is_startup(segment, match.start()):
                startup.append(value)
            else:
                ordinary.append(value)
    return ordinary, startup, has_ordinary_cue


def _supported_year_system(field: StudyDurationField, answer: str) -> bool:
    condition = field.condition.strip()
    if field.kind not in (ORDINARY_MAX, STARTUP_MAX) or not condition.endswith("年制"):
        return False
    target = _to_int(condition[:-2])
    if not _valid_number(target):
        return False
    text = _blank_quotes(answer)
    parens = _paren_spans(text)
    conditions = _conditions(text)
    if not any(_to_int(label[:-2]) == target for _, label in conditions):
        return False
    initial = text[:conditions[0][0]]
    ordinary_topic = any(
        _segment_has_ordinary_topic(segment) for segment in _BREAK.split(initial)
    )
    found = False
    for index, (position, label) in enumerate(conditions):
        end = conditions[index + 1][0] if index + 1 < len(conditions) else len(text)
        ordinary, startup, has_cue = _branch_values(
            text[position:end], position, parens
        )
        if has_cue:
            ordinary_topic = True
        if _to_int(label[:-2]) == target and (
            (field.kind == ORDINARY_MAX and ordinary_topic and ordinary)
            or (field.kind == STARTUP_MAX and startup)
        ):
            found = True
    return found


def _clauses(text: str) -> list[tuple[str, bool]]:
    clauses: list[tuple[str, bool]] = []
    start = 0
    strong = True
    for match in _BREAK.finditer(text):
        piece = text[start:match.start()]
        if piece.strip():
            clauses.append((piece, strong))
        strong = match.group(0) in _STRONG_BREAKS
        start = match.end()
    piece = text[start:]
    if piece.strip():
        clauses.append((piece, strong))
    return clauses


def _property_value(text: str, cue: re.Pattern[str]) -> int | None:
    match = cue.search(text)
    if not match:
        return None
    return _duration_at(text, match.end())


def _supported_mode(field: StudyDurationField, answer: str) -> bool:
    mode = field.condition.strip()
    if mode not in ("全日制", "非全日制") or field.kind not in (BASIC, MAXIMUM):
        return False
    text = _blank_quotes(answer)
    all_modes = _mode_names(text)
    if mode not in all_modes:
        return False
    assigned: set[tuple[str, str]] = set()
    shared_modes: list[str] = []
    group_modes: set[str] = set()
    for piece, strong in _clauses(text):
        if strong:
            group_modes = set()
        if _is_uncertain(piece):
            continue
        modes = _mode_names(piece)
        group_modes.update(modes)
        shared = bool(_SHARED.search(piece))
        values: list[tuple[str, int]] = []
        basic = _property_value(piece, _BASIC_CUE)
        maximum = _property_value(piece, _MAX_CUE)
        if basic is not None:
            values.append((BASIC, basic))
        if maximum is not None:
            values.append((MAXIMUM, maximum))
        if not values:
            continue
        if len(modes) >= 2:
            if shared:
                for item in modes:
                    assigned.update((item, kind) for kind, _ in values)
                shared_modes = list(modes)
        elif len(modes) == 1:
            assigned.update((modes[0], kind) for kind, _ in values)
        else:
            targets = shared_modes if shared and shared_modes else []
            if not targets and len(group_modes) == 1:
                targets = list(group_modes)
            if not targets and len(all_modes) == 1:
                targets = all_modes
            for item in targets:
                assigned.update((item, kind) for kind, _ in values)
    return (mode, field.kind) in assigned


def study_duration_supported(field: StudyDurationField, scoped_answer: str) -> bool:
    if not isinstance(field, StudyDurationField):
        return False
    if not isinstance(scoped_answer, str) or not scoped_answer.strip():
        return False
    if field.kind in (ORDINARY_MAX, STARTUP_MAX):
        return _supported_year_system(field, scoped_answer)
    if field.kind in (BASIC, MAXIMUM):
        return _supported_mode(field, scoped_answer)
    return False

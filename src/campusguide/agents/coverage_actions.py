"""Structured action/result semantics for clause-level coverage.

The caller passes a requirement and the scoped final claim text. This module
answers only a small set of reliably parsed semantic shapes and returns
``None`` for everything else so the caller can keep its legacy matcher.
It does not check sources, subjects, classes, or versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from campusguide.verification.facts import chinese_to_int

_CJK = r"[\u4e00-\u9fff]"
_CN_NUM = r"(?:\d+|[〇零一二三四五六七八九十百千万亿两]+)"
_UNIT = r"(?:个)?(?:工作日|自然日|天|日|周|星期|月)"
_RANGE_SEP = r"(?:至|到|~|～|—|–|-|－)"
_APPROVAL_VERB = r"(?:审批|批准|核准|审定|审核|同意)"


@dataclass(frozen=True)
class _Range:
    kind: str
    low: int
    high: int
    unit: str


_RANGE_PATTERN = re.compile(rf"({_CN_NUM})\s*{_RANGE_SEP}\s*({_CN_NUM})\s*({_UNIT})")
_UPPER_PREFIX_PATTERN = re.compile(rf"(?:不超过|不多于|至多|最长)\s*({_CN_NUM})\s*({_UNIT})")
_UPPER_SUFFIX_PATTERN = re.compile(rf"({_CN_NUM})\s*({_UNIT})\s*(?:以内|以下|之内|内|前)")
_LOWER_PREFIX_PATTERN = re.compile(rf"(?:不少于|不低于|至少|最短)\s*({_CN_NUM})\s*({_UNIT})")
_LOWER_SUFFIX_PATTERN = re.compile(rf"({_CN_NUM})\s*({_UNIT})\s*(?:以上|及以上|以外|之外|起)")
_EXACT_PATTERN = re.compile(rf"({_CN_NUM})\s*({_UNIT})")


def _canonical_unit(unit: str) -> str:
    unit = unit.replace("个", "")
    if unit == "日":
        return "天"
    if unit == "星期":
        return "周"
    return unit


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return chinese_to_int(token)


def _interval(span: _Range) -> tuple[int | None, int | None]:
    if span.kind == "max":
        return None, span.high
    if span.kind == "min":
        return span.low, None
    return span.low, span.high


def _range_covers(asserted: _Range, requested: _Range) -> bool:
    """The requested policy interval must fit inside the asserted branch."""
    if asserted.unit != requested.unit:
        return False
    asserted_low, asserted_high = _interval(asserted)
    requested_low, requested_high = _interval(requested)
    if asserted_low is not None and (
        requested_low is None or requested_low < asserted_low
    ):
        return False
    return not (
        asserted_high is not None
        and (requested_high is None or requested_high > asserted_high)
    )


def _range_ok(span: _Range | None, text: str) -> bool:
    if span is None:
        return True
    asserted = _parse_range(text)
    return asserted is not None and _range_covers(asserted, span)


def _parse_range(text: str) -> _Range | None:
    match = _RANGE_PATTERN.search(text)
    if match:
        low = _to_int(match.group(1))
        high = _to_int(match.group(2))
        if low is not None and high is not None and low <= high:
            return _Range("range", low, high, _canonical_unit(match.group(3)))

    for pattern, kind in (
        (_UPPER_PREFIX_PATTERN, "max"),
        (_UPPER_SUFFIX_PATTERN, "max"),
        (_LOWER_PREFIX_PATTERN, "min"),
        (_LOWER_SUFFIX_PATTERN, "min"),
    ):
        match = pattern.search(text)
        if match:
            value = _to_int(match.group(1))
            if value is not None:
                return _Range(kind, value, value, _canonical_unit(match.group(2)))

    match = _EXACT_PATTERN.search(text)
    if match:
        value = _to_int(match.group(1))
        if value is not None:
            return _Range("exact", value, value, _canonical_unit(match.group(2)))
    return None


_STRONG_SPLIT = re.compile(r"[。！？!?；;\n]+")
_COMMA_SPLIT = re.compile(r"[，,]+")
_OPERATIONAL_CUE = re.compile(
    r"申诉|复查|复核|复审|审批|批准|核准|审定|审核|同意|终止|结束|继续|成绩|考核|考试"
)


def _strong_clauses(text: str) -> list[str]:
    return [part.strip() for part in _STRONG_SPLIT.split(text) if part.strip()]


def _has_deadline_value(text: str) -> bool:
    return _parse_range(text) is not None or bool(
        re.search(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?", text)
    )


_UNCERTAINTY = re.compile(
    r"尚不明确|不明确|未明确|未予明确|尚待明确|有待明确|尚不确定|不确定|"
    r"无法确定|不能确定|无法判断|不能判断|无法确认|不能确认|不得而知|"
    r"尚不清楚|不清楚|未知|不详|未提及|没有提及|未涉及|未说明|未规定|"
    r"没有规定|未作规定|尚无定论|证据不足|材料不足|待定|待明确|另行明确"
)
_QUESTION_ECHO = re.compile(
    r"(?<!无论)(?<!不管)(?<!不论)(?:能否|是否|可否|能不能|可不可以)|[吗嘛]\s*$"
)


def _is_uncertain(text: str) -> bool:
    return bool(_UNCERTAINTY.search(text) or _QUESTION_ECHO.search(text))


def _merge_deadline_fragments(parts: list[str]) -> list[str]:
    merged: list[str] = []
    index = 0
    while index < len(parts):
        current = parts[index]
        if index + 1 < len(parts):
            following = parts[index + 1]
            current_deadline = _has_deadline_value(current)
            following_deadline = _has_deadline_value(following)
            current_cue = bool(_OPERATIONAL_CUE.search(current))
            following_cue = bool(_OPERATIONAL_CUE.search(following))
            if (current_deadline and not current_cue and following_cue and not following_deadline) or (
                following_deadline and not following_cue and current_cue and not current_deadline
            ):
                merged.append(f"{current}，{following}")
                index += 2
                continue
        merged.append(current)
        index += 1
    return merged


def _comma_branches(text: str) -> list[str]:
    merged: list[str] = []
    for chunk in _STRONG_SPLIT.split(text):
        parts = [part.strip() for part in _COMMA_SPLIT.split(chunk) if part.strip()]
        merged.extend(_merge_deadline_fragments(parts))
    return merged


_SELECTION_UNSELECTED = re.compile(
    r"(?:未|没|没有)[^，。；;]{0,8}(?:选课|选修)|"
    r"(?:未办理|未完成|未取得)[^，。；;]{0,8}选课|自行听课|擅自听课"
)
_SELECTION_SELECTED = re.compile(r"已选课|已选课程|选了课|已办理选课|已选修|选课后|已选")
_ASSESSMENT = re.compile(r"考核|考试|测试|成绩|分数|评分")
_HANDLING = re.compile(
    r"怎么|如何|怎样|处理|算|有效|无效|合格|不合格|通过|认定|登记|后果|影响|"
    r"能否|是否|可以|吗|么"
)
_GRADE_TOPIC = re.compile(r"成绩|分数|登记|记分|结果")
_GRADE_RESULT = re.compile(
    r"(?<![0-9〇零一二三四五六七八九十百千万亿两])0\s*分|零分|缺考|"
    r"(?:不予|不得|不能|无法|不准|不)[^，。；;]{0,8}"
    r"(?:登记|记录|记入|计入|认可|认定|保留|计算)[^，。；;]{0,8}"
    r"(?:成绩|考核|考试|分数)|"
    r"(?:成绩|分数|考核|考试)[^，。；;]{0,12}"
    r"(?:0\s*分|零分|缺考|无效|作废|取消|不予|不得|不能|无法|"
    r"登记|记入|计入|计为|记为|认定为|视为|有效|合格|不合格|通过|不通过)|"
    r"(?:登记|记入|计入|计为|记为|认定为|按)[^，。；;]{0,8}"
    r"(?:0\s*分|零分|缺考|不合格|成绩)|"
    r"(?:成绩|分数)[^，。；;]{0,6}(?:有效|无效|合格|不合格|通过|不通过)"
)
_ASSESSMENT_ELIGIBILITY_RESULT = re.compile(
    r"(?:不得|不能|不准|不允许|不可以|可以|可|允许|准予|能|须|应当|应)"
    r"[^，。；;]{0,4}(?:参加|申请|报名|进行|组织)"
    r"[^，。；;]{0,2}(?:考核|考试|测试|补考|重考)"
)


@dataclass(frozen=True)
class _SelectionRequest:
    state: str
    outcome: str


def _selection_request(requirement: str) -> _SelectionRequest | None:
    if not _ASSESSMENT.search(requirement):
        return None
    state = _selection_state(requirement)
    if state is None:
        return None
    if _GRADE_TOPIC.search(requirement) and _HANDLING.search(requirement):
        return _SelectionRequest(state, "grade")
    if _HANDLING.search(requirement):
        return _SelectionRequest(state, "eligibility")
    return None


def _selection_states(text: str) -> set[str]:
    states: set[str] = set()
    if _SELECTION_UNSELECTED.search(text):
        states.add("unselected")
    if _SELECTION_SELECTED.search(text):
        states.add("selected")
    return states


def _selection_state(text: str) -> str | None:
    states = _selection_states(text)
    if len(states) != 1:
        return None
    return next(iter(states))


def _selection_answered(request: _SelectionRequest, answer: str) -> bool:
    current_state: str | None = None
    for strong in _strong_clauses(answer):
        parts = [part.strip() for part in _COMMA_SPLIT.split(strong) if part.strip()]
        for part in parts:
            if _is_uncertain(part):
                continue
            states = _selection_states(part)
            if len(states) == 1:
                current_state = next(iter(states))
            elif len(states) > 1:
                current_state = None
            if current_state != request.state:
                continue
            if request.outcome == "grade" and _GRADE_RESULT.search(part):
                return True
            if request.outcome == "eligibility" and _ASSESSMENT_ELIGIBILITY_RESULT.search(part):
                return True
    return False


_AGENCY_QUESTION = re.compile(
    rf"(?:由|经|报|请|需|须|要|需要)\s*谁\s*{_APPROVAL_VERB}|"
    rf"谁\s*(?:能|可以|有权|负责)?\s*{_APPROVAL_VERB}|"
    r"(?:批准|审批|核准|审定|审核)[^。；;]{0,8}(?:由|经)?\s*谁(?:\s*负责)?|"
    r"(?:批准|审批|核准|审定|审核)(?:机关|部门|单位|机构)|"
    r"(?:哪个|哪一个|何种|什么)(?:部门|机关|单位|机构)"
)
_EXPLICIT_AGENCY = re.compile(
    rf"(?:(?<!申)由|经|(?<!申)报|呈|交|向|"
    rf"(?<!申)请(?!假|示|求|问|柬|客|战|缨))"
    rf"\s*({_CJK}{{2,20}}?)(?:负责)?\s*{_APPROVAL_VERB}"
)
_AGENCY_SUFFIX = r"(?:办公室|委员会|中心|学院|学部|处|室|部|科|系|所|办|局|厅|署)"
_SUFFIX_AGENCY = re.compile(
    rf"({_CJK}{{2,24}}?{_AGENCY_SUFFIX})\s*(?:负责)?\s*{_APPROVAL_VERB}"
)
_AGENCY_STOP = {
    "内部",
    "外部",
    "上级",
    "下级",
    "相关",
    "有关",
    "相应",
    "同级",
    "其他",
    "其它",
    "部分",
    "全部",
    "自行",
    "各部门",
    "有关方面",
    "谁",
}


_AUDIENCE_TOKEN = re.compile(
    r"(?:全日制|非全日制)?(?:"
    r"(?:硕士|博士)研究生|"
    r"(?:本科|专科|硕士|博士)(?:学生|生)|"
    r"研究生"
    r")(?!院|部|处|科|系|中心|工作)"
)
_AUDIENCE_PREFIX = re.compile(
    r"^(?:(?:全日制|非全日制)?(?:硕士|博士)研究生|"
    r"(?:全日制|非全日制)?(?:本科|专科|硕士|博士)(?:学生|生)|"
    r"(?:全日制|非全日制)?研究生|"
    r"(?:全日制|非全日制)?学生)"
)


def _canonical_audience(token: str) -> str:
    token = token.replace("非全日制", "").replace("全日制", "")
    for key in ("本科", "专科", "博士", "硕士", "研究生"):
        if key in token:
            return f"{key}生" if key != "研究生" else key
    return token


def _audiences_in(text: str) -> set[str]:
    return {_canonical_audience(match.group(0)) for match in _AUDIENCE_TOKEN.finditer(text)}


def _requested_population(text: str) -> str | None:
    found = _audiences_in(text)
    if len(found) != 1:
        return None
    return next(iter(found))


@dataclass(frozen=True)
class _AuthorityRequest:
    range_spec: _Range | None
    action: str
    population: str | None = None


def _authority_request(requirement: str) -> _AuthorityRequest | None:
    if not re.search(_APPROVAL_VERB, requirement) or not _AGENCY_QUESTION.search(requirement):
        return None
    action = _authority_action(requirement)
    if action is None:
        return None
    return _AuthorityRequest(
        _parse_range(requirement), action, _requested_population(requirement)
    )


_NEGATION_TAIL = re.compile(
    r"(?:未|未能|没|没能|不|无|非|没有|无须|无需|不需|不需要|不必|不须|不应|不应该|"
    r"不是|并非|不予|不得(?!不)|不能(?!不)|不准)"
    r"(?:由|经|报|呈|交|向|请)?$"
)
_NEGATION_WORD = re.compile(
    r"(?:未|未能|没|没有|无须|无需|不必|不需|不需要|不须|不应|不应该|不是|并非|"
    r"不予|不得(?!不)|不能(?!不)|不准)"
)


def _negated_before(text: str, start: int) -> bool:
    return bool(_NEGATION_TAIL.search(text[max(0, start - 8) : start]))


def _looks_like_agency(candidate: str) -> bool:
    candidate = candidate.strip(" ，,。；;：:")
    if len(candidate) < 2 or candidate in _AGENCY_STOP:
        return False
    if re.search(r"备案|申请", candidate):
        return False
    if _NEGATION_WORD.search(candidate):
        return False
    if re.search(r"(?:审批|批准|核准|审定|审核|同意)$", candidate):
        return False
    return not re.search(r"(?:须|需|要|应|当|将|可|能|由|经|报|呈|交|向|请|的|了)$", candidate)


_RANGE_EXPR = re.compile(
    rf"(?:{_CN_NUM}\s*{_RANGE_SEP}\s*{_CN_NUM}\s*{_UNIT}|"
    rf"(?:不超过|不多于|至多|最长)\s*{_CN_NUM}\s*{_UNIT}|"
    rf"(?:不少于|不低于|至少|最短)\s*{_CN_NUM}\s*{_UNIT}|"
    rf"{_CN_NUM}\s*{_UNIT}\s*(?:以内|以下|之内|内|前|以上|及以上|以外|之外|起)|"
    rf"{_CN_NUM}\s*{_UNIT})"
)
_ACTION_LEADING = re.compile(
    r"^(?:如果|假如|倘若|关于|对于|请问|那么|仍然|要求|需要|应当|必须|还是|"
    r"仍|还|再|又|需|须|应|要)+"
)
_ACTION_TRAILING = re.compile(r"(?:由|经|报|呈|交|向|需要|需|须|要|应当|必须)$")
_APPROVAL_LEADING = re.compile(
    rf"^(?:{_APPROVAL_VERB}|负责|由|经|报|呈|交|向|请|需|须|要|需要|应当|应|必须|的|是)+"
)
_ACTION_STOP = re.compile(
    r"谁|什么|哪个|哪些|何种|部门|机关|单位|机构|是否|能否|可否|如何|怎么|怎样|多久|多长时间"
)
_OPPOSITE_BEFORE = re.compile(
    r"(?:取消|撤销|停止|终止|暂停|暂缓|不得|不能|不准|不允许|不可以|不许|"
    r"禁止|严禁|无需|无须|不必|不再|没有|不予|未予|[停不未没无非禁])$"
)


_BRACKET_CONTENT = re.compile(r"[（(][^）)]*[）)]")


def _clean_action_candidate(text: str) -> str:
    candidate = text.strip(" ，,。；;：:、！!？?")
    candidate = _ACTION_LEADING.sub("", candidate)
    candidate = _BRACKET_CONTENT.sub("", candidate)
    candidate = _RANGE_EXPR.sub("", candidate)
    candidate = candidate.strip()
    for _ in range(3):
        updated = _ACTION_TRAILING.sub("", candidate)
        if updated == candidate:
            break
        candidate = updated
    if candidate.endswith("请") and not candidate.endswith("申请"):
        candidate = candidate[:-1]
    return candidate.strip()


def _valid_action(candidate: str) -> bool:
    if not (2 <= len(candidate) <= 16):
        return False
    if not re.fullmatch(rf"{_CJK}{{2,16}}", candidate):
        return False
    return not _ACTION_STOP.search(candidate)


def _normalize_business(candidate: str) -> str:
    stripped = _AUDIENCE_PREFIX.sub("", candidate, count=1).strip()
    if stripped and _valid_action(stripped):
        return stripped
    return candidate


def _authority_action(requirement: str) -> str | None:
    match = _AGENCY_QUESTION.search(requirement)
    if match is None:
        return None
    before = requirement[: match.start()]
    before = re.split(r"[，,。；;！？!?\n]", before)[-1]
    candidate = _normalize_business(_clean_action_candidate(before))
    if _valid_action(candidate):
        return candidate
    after = requirement[match.end() :]
    after = re.split(r"[，,。；;！？!?\n]", after)[0]
    after = _APPROVAL_LEADING.sub("", after.strip())
    candidate = _normalize_business(_clean_action_candidate(after))
    if _valid_action(candidate):
        return candidate
    return None


def _action_present(action: str, text: str) -> bool:
    for match in re.finditer(re.escape(action), text):
        before = text[max(0, match.start() - 4) : match.start()]
        if _OPPOSITE_BEFORE.search(before):
            continue
        return True
    return False


def _approval_agency_matches(branch: str) -> list[tuple[re.Match[str], bool]]:
    matches: list[tuple[re.Match[str], bool]] = []
    for start in range(len(branch)):
        match = _EXPLICIT_AGENCY.match(branch, start)
        if match is None:
            continue
        if _negated_before(branch, match.start()):
            continue
        if _looks_like_agency(match.group(1)):
            matches.append((match, True))
    for match in _SUFFIX_AGENCY.finditer(branch):
        if _negated_before(branch, match.start()):
            continue
        if _looks_like_agency(match.group(1)):
            matches.append((match, False))
    return matches


_ADMIN_FILLER_LEAD = re.compile(r"^(?:同时|并|还|另|随后|然后)?(?:报|呈|交|向|由|经|备案)")


def _business_candidate(text: str) -> str | None:
    candidate = _normalize_business(_clean_action_candidate(text))
    if _AUDIENCE_TOKEN.fullmatch(candidate) or not _valid_action(candidate):
        return None
    return candidate


def _part_business(part: str) -> str | None:
    matches = _approval_agency_matches(part)
    explicit = [match for match, is_explicit in matches if is_explicit]
    if explicit:
        return _business_candidate(part[: explicit[0].start()])
    if _ADMIN_FILLER_LEAD.match(part):
        return None
    return _business_candidate(_BRACKET_CONTENT.sub("", part))


_CONDITION_CUE = re.compile(
    r"(?:而|又|且|并|同时|仍然|仍|还|再|另|需要|需|须|要|应当|应该|应|必须)+"
)


def _conditional_business(text: str) -> str | None:
    for match in reversed(list(_CONDITION_CUE.finditer(text))):
        tail = text[match.end() :].strip(" ，,。；;：:、！!？?")
        if tail.endswith("的"):
            tail = tail[:-1]
        candidate = _normalize_business(_clean_action_candidate(tail))
        if _valid_action(candidate):
            return candidate
    return None


def _authority_answered(request: _AuthorityRequest, answer: str) -> bool:
    business: str | None = None
    population: str | None = None
    for strong in _strong_clauses(answer):
        carried_range_ok: bool | None = None
        conditional_business: str | None = None
        parts = [part.strip() for part in _COMMA_SPLIT.split(strong) if part.strip()]
        for part in parts:
            if _is_uncertain(part):
                continue
            part_populations = _audiences_in(part)
            if part_populations:
                if len(part_populations) == 1:
                    new_population = next(iter(part_populations))
                    if new_population != population:
                        population = new_population
                        business = None
                        carried_range_ok = None
                        conditional_business = None
                else:
                    population = None
                    business = None
                    carried_range_ok = None
                    conditional_business = None
            part_business = _part_business(part)
            if part_business is not None and part_business != business:
                business = part_business
                carried_range_ok = None
            if part_business is not None and part_business != conditional_business:
                conditional_business = None
            elif part_business is None and not _approval_agency_matches(part):
                extracted = _conditional_business(part)
                if extracted is not None:
                    conditional_business = extracted
            if _parse_range(part) is not None:
                carried_range_ok = _range_ok(request.range_spec, part)
            part_range_ok = (
                carried_range_ok
                if carried_range_ok is not None
                else _range_ok(request.range_spec, part)
            )
            population_ok = (
                request.population is None
                or population is None
                or population == request.population
            )
            matches = _approval_agency_matches(part)
            if not matches:
                continue
            explicit = [match for match, is_explicit in matches if is_explicit]
            if not explicit:
                if (
                    part_range_ok
                    and population_ok
                    and (
                        _action_present(request.action, part)
                        or (
                            business is not None
                            and _action_present(request.action, business)
                        )
                    )
                ):
                    return True
                continue
            for match in explicit:
                prefix = part[: match.start()]
                candidate = _normalize_business(_clean_action_candidate(prefix))
                if _valid_action(candidate):
                    if (
                        part_range_ok
                        and population_ok
                        and _action_present(request.action, candidate)
                    ):
                        return True
                    continue
                if candidate:
                    continue
                if (
                    part_range_ok
                    and population_ok
                    and _action_present(request.action, prefix)
                ):
                    return True
                if (
                    part_range_ok
                    and population_ok
                    and _action_present(request.action, part[match.end() :])
                ):
                    return True
                if (
                    part_range_ok
                    and population_ok
                    and conditional_business is not None
                    and _action_present(request.action, conditional_business)
                ):
                    return True
                if (
                    part_range_ok
                    and population_ok
                    and business is not None
                    and _action_present(request.action, business)
                ):
                    return True
    return False


_PROGRAM_NOUNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("微专业", re.compile(r"微专业")),
    ("辅修", re.compile(r"辅修")),
    ("双学位", re.compile(r"双学位")),
    ("第二学位", re.compile(r"第二学位")),
)
_CONTINUATION_CUE = re.compile(
    r"(?:还能|能否|是否|可否|可以|会|仍可|仍能)[^。；;]{0,8}(?:继续(?!教育)|终止|中止)|"
    r"(?:继续(?!教育)|终止|中止)[^。；;]{0,2}(?:吗|么|嘛|否)"
)
_CONTINUATION_RESULT = re.compile(
    r"终止(?!条件|流程|手续|规定|时间)|结束(?!时间|条件|流程)|"
    r"不再继续|不能继续|无法继续|停止(?!条件|流程)|中止|失效(?!条件)|取消|"
    r"可以继续|可继续|能继续|仍可继续|仍能继续|仍继续|继续有效|继续学习|继续修读"
)
_INCOMPLETE = re.compile(
    r"(?:没|未|没有)(?:修满|完成|达到|取得|获得)[^，。；;]{0,12}(?:学分|要求)|"
    r"学分不足|未取得[^，。；;]{0,8}学分"
)
_OUTCOME_CUE = re.compile(
    r"能得到什么|获得什么|怎么办|怎么处理|如何处理|什么结果|什么后果|"
    r"发什么|给什么|能否|可以吗|吗|什么"
)
_COMPLETION = re.compile(
    r"(?:修满|完成|达到|合格|通过|已修满)[^，。；;]{0,15}"
    r"(?:学分|要求|学业|课程|结业|毕业|培养方案)|"
    r"(?:结业|毕业)(?:后|之后|时)|"
    r"(?:学分|要求)[^，。；;]{0,8}(?:修满|完成|达到|合格|通过)"
)
_OUTCOME_RESULT = re.compile(
    r"(?:发放|颁发|出具|提供|开具|发给|授予|寄发|发(?:放|给)?)"
    r"[^，。；;]{0,10}(?:成绩单|成绩证明|修读证明|证明|证书|学位|毕业证书|结业证书)|"
    r"(?:成绩单|成绩证明|修读证明|证书|学位|毕业证书|结业证书)"
    r"[^，。；;]{0,8}(?:发放|颁发|出具|提供|开具|发给|授予|不予|不发|不颁发|不提供|不出具)|"
    r"(?:不予|不得|不能|无法|取消)[^，。；;]{0,8}"
    r"(?:毕业|结业|发(?:放|给)?|颁发|授予|出具|提供|登记|记录|记入|计入)|"
    r"(?:不发|不颁发|不授予|不提供|不出具|不予)[^，。；;]{0,8}"
    r"(?:证明|证书|成绩单|学位|毕业|结业)|"
    r"退费|补修|重修|"
    r"(?:予以|准予)[^，。；;]{0,4}(?:结业|毕业)|"
    r"(?:作|按|以|做)[^，。；;]{0,4}(?:结业|毕业)(?:处理|对待|办理|离校)|"
    r"(?:结业|毕业)(?:处理|对待)"
)
_SCHOOL_SUBJECT = re.compile(r"学校|本校|校内")
_OTHER_PROJECT = re.compile(r"(?:其他|其它|另一|别的)(?:项目|专业|学位|课程)")
_MAIN_PROGRAM = re.compile(r"主修")
_SAFE_REFERENCE = re.compile(
    r"^\s*(?:其(?!他|它)|该(?:专业|课程|学习)?|此(?:专业|课程|学习)?|"
    r"上述(?:专业|课程|学习)?|学习|修读|课程|学业)"
)


def _requested_program(text: str) -> str | None:
    found = [key for key, pattern in _PROGRAM_NOUNS if pattern.search(text)]
    if len(found) == 1:
        return found[0]
    return None


def _program_spans(program: str, text: str) -> list[tuple[int, int]]:
    for key, pattern in _PROGRAM_NOUNS:
        if key == program:
            return [(match.start(), match.end()) for match in pattern.finditer(text)]
    return []


def _program_present(program: str, text: str) -> bool:
    for start, end in _program_spans(program, text):
        before = text[max(0, start - 12) : start]
        after = text[end : end + 4]
        if re.search(r"(?:除(?:了)?|非|不是|不属于|不含)[^，。；;]{0,12}$", before):
            continue
        if re.match(r"(?:以外|之外|除外)", after):
            continue
        return True
    return False


def _other_program_present(program: str, text: str) -> bool:
    return any(key != program and pattern.search(text) for key, pattern in _PROGRAM_NOUNS)


def _program_request(requirement: str) -> tuple[str, str] | None:
    program = _requested_program(requirement)
    if program is None:
        return None
    if _INCOMPLETE.search(requirement) and _OUTCOME_CUE.search(requirement):
        return ("outcome", program)
    if _CONTINUATION_CUE.search(requirement):
        return ("continue", program)
    return None


def _safe_continuation_reference(program: str, part: str) -> bool:
    if (
        _MAIN_PROGRAM.search(part)
        or _other_program_present(program, part)
        or _OTHER_PROJECT.search(part)
    ):
        return False
    return bool(_SAFE_REFERENCE.match(part))


def _continuation_answered(program: str, answer: str) -> bool:
    for strong in _strong_clauses(answer):
        parts = [part.strip() for part in _COMMA_SPLIT.split(strong) if part.strip()]
        previous_program = False
        for part in parts:
            if _is_uncertain(part):
                previous_program = False
                continue
            has_program = _program_present(program, part)
            has_result = bool(_CONTINUATION_RESULT.search(part))
            if has_program and has_result:
                return True
            if (
                has_result
                and not has_program
                and previous_program
                and _safe_continuation_reference(program, part)
            ):
                return True
            if has_program:
                previous_program = True
            elif (
                _MAIN_PROGRAM.search(part)
                or _other_program_present(program, part)
                or _OTHER_PROJECT.search(part)
            ):
                previous_program = False
    return False


def _outcome_subject(program: str, branch: str) -> bool:
    if _program_present(program, branch):
        return True
    if not _SCHOOL_SUBJECT.search(branch):
        return False
    return not (_other_program_present(program, branch) or _OTHER_PROJECT.search(branch))


def _outcome_answered(program: str, answer: str) -> bool:
    for branch in _strong_clauses(answer):
        if _is_uncertain(branch):
            continue
        if not _outcome_subject(program, branch):
            continue
        if _COMPLETION.search(branch) and not _INCOMPLETE.search(branch):
            continue
        if _OUTCOME_RESULT.search(branch):
            return True
    return False


_DEADLINE_WORD = re.compile(
    r"期限|时限|多久|几天|几日|几日内|多少日|多少天|多长时间|什么时候|截止"
)
_APPEAL_TOKEN = r"(?<!接到)(?<!受理)(?<!对)(?<!就)申诉(?!处理委员会)"
_APPEAL_REQ_ACTION = re.compile(_APPEAL_TOKEN)
_REVIEW_REQ_ACTION = re.compile(r"复查|复核|复审")
_PROVINCE_STAGE = re.compile(r"省教育厅|教育厅|省级教育行政部门|省教育行政部门|省教育部门")
_SCHOOL_STAGE = re.compile(r"校内|学校|本校|校学生申诉处理委员会|学生申诉处理委员会|委员会")
_APPEAL_ACTION = re.compile(
    rf"向[^，。；;]{{0,30}}?{_APPEAL_TOKEN}|"
    rf"(?:提出|书面|提交|申请|可|应|须|应当|需)[^，。；;]{{0,6}}?{_APPEAL_TOKEN}|"
    rf"{_APPEAL_TOKEN}(?:期限|时限|书|权|的)"
)
_REVIEW_ACTION = re.compile(r"复查|复核|复审")
_REVIEW_BINDING_ACTION = re.compile(r"(?:复查|复核|复审)(?!决定书)")
_TEMPORAL_ANCHOR = re.compile(
    r"^(?:在|自|从|于)[^，。；;]{0,40}?"
    r"(?:之日起|之日|之后|以后|起|后)"
    r"[^，。；;]{0,12}?(?:以内|之内|内|之前|前|止)$"
)
_TRIGGER_DOCUMENT = re.compile(
    r"(?:复查|复核|复审|申诉|处理|处分|退学)?(?:决定书|结论书|通知书|告知书)"
)


@dataclass(frozen=True)
class _AppealRequest:
    action: str
    stage: str


def _stage_near_action(text: str, start: int, end: int) -> str | None:
    best: tuple[int, int, str] | None = None
    for stage, pattern in (("province", _PROVINCE_STAGE), ("school", _SCHOOL_STAGE)):
        for match in pattern.finditer(text):
            if match.end() <= start:
                distance = start - match.end()
                order = 0
            elif match.start() >= end:
                distance = match.start() - end
                order = 1
            else:
                continue
            if distance > 30:
                continue
            candidate = (distance, order, stage)
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else None


def _appeal_request(requirement: str) -> _AppealRequest | None:
    deadline = _DEADLINE_WORD.search(requirement)
    if deadline is None:
        return None

    candidates: list[tuple[int, int, str]] = []
    for action, pattern in (("appeal", _APPEAL_REQ_ACTION), ("review", _REVIEW_REQ_ACTION)):
        for match in pattern.finditer(requirement):
            candidates.append((match.start(), match.end(), action))
    if not candidates:
        return None

    before = [item for item in candidates if item[1] <= deadline.start()]
    if before:
        chosen = max(before, key=lambda item: item[1])
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        after = [item for item in candidates if item[0] >= deadline.end()]
        if not after:
            return None
        chosen = min(after, key=lambda item: item[0])

    stage = _stage_near_action(requirement, chosen[0], chosen[1])
    if stage is None:
        return None
    return _AppealRequest(chosen[2], stage)


def _branch_stage(branch: str) -> str | None:
    if _PROVINCE_STAGE.search(branch):
        return "province"
    if _SCHOOL_STAGE.search(branch):
        return "school"
    return None


def _has_action(branch: str, pattern: re.Pattern[str]) -> bool:
    matches = list(pattern.finditer(branch))
    for match in matches:
        if any(
            other is not match
            and other.start() <= match.start()
            and match.end() <= other.end()
            for other in matches
        ):
            continue
        if not _negated_before(branch, match.start()):
            return True
    return False


def _has_appeal_action(branch: str) -> bool:
    return _has_action(branch, _APPEAL_ACTION)


def _has_review_action(branch: str) -> bool:
    return _has_action(branch, _REVIEW_ACTION)


def _has_review_binding_action(branch: str) -> bool:
    return _has_action(branch, _REVIEW_BINDING_ACTION)


def _request_action_present(request: _AppealRequest, branch: str) -> bool:
    if request.action == "appeal":
        return _has_appeal_action(branch)
    return _has_review_binding_action(branch)


def _is_temporal_anchor(branch: str) -> bool:
    if not _TEMPORAL_ANCHOR.match(branch):
        return False
    masked = _TRIGGER_DOCUMENT.sub("", branch)
    return not (_has_appeal_action(masked) or _has_review_binding_action(masked))


def _adjacent_deadline_answered(request: _AppealRequest, answer: str) -> bool:
    for chunk in _STRONG_SPLIT.split(answer):
        parts = [part.strip() for part in _COMMA_SPLIT.split(chunk) if part.strip()]
        for index, part in enumerate(parts):
            if not _is_temporal_anchor(part) or _is_uncertain(part):
                continue
            neighbours = parts[index + 1 : index + 2] + parts[max(0, index - 1) : index]
            for neighbour in neighbours:
                if _is_uncertain(neighbour):
                    continue
                if _branch_stage(neighbour) != request.stage:
                    continue
                if not _request_action_present(request, neighbour):
                    continue
                if _has_deadline_value(neighbour):
                    continue
                return True
    return False


def _appeal_answered(request: _AppealRequest, answer: str) -> bool:
    for branch in _comma_branches(answer):
        if _is_uncertain(branch):
            continue
        if _branch_stage(branch) != request.stage:
            continue
        if request.action == "appeal":
            if not _has_appeal_action(branch):
                continue
        elif not _has_review_binding_action(branch):
            continue
        if _has_deadline_value(branch):
            return True
    return _adjacent_deadline_answered(request, answer)


def requirement_answered(requirement: str, answer: str) -> bool | None:
    """Answer a recognized structured request; ``None`` defers to legacy matching.

    ``True`` and ``False`` only mean that the scoped answer text does or does
    not contain the requested action/result branch. Source, subject, class and
    version gates remain the caller's responsibility.
    """
    requirement_text = requirement or ""
    answer_text = answer or ""
    outcomes: list[bool] = []

    appeal = _appeal_request(requirement_text)
    if appeal is not None:
        outcomes.append(_appeal_answered(appeal, answer_text))

    selection = _selection_request(requirement_text)
    if selection is not None:
        outcomes.append(_selection_answered(selection, answer_text))

    program_request = _program_request(requirement_text)
    if program_request is not None:
        shape, program = program_request
        if shape == "continue":
            outcomes.append(_continuation_answered(program, answer_text))
        else:
            outcomes.append(_outcome_answered(program, answer_text))

    authority = _authority_request(requirement_text)
    if authority is not None:
        outcomes.append(_authority_answered(authority, answer_text))

    if len(outcomes) != 1:
        return None
    return outcomes[0]

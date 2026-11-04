"""Deterministic condition checks between a claim and its quoted evidence (R1).

The semantic verifier is a language model and can pass a claim whose quoted
evidence states the opposite condition (report case 001: "开考30分钟后" written as
"开考后30分钟内"). These helpers extract *condition assertions* deterministically
and compare them:

  * time thresholds with a comparison direction (后 / 内 / 前 / 以上 / 以下);
  * modality of an obligation (禁止 / 应当 / 可以);
  * role assignment (第一导师 / 第二导师 and similar pairs);
  * membership of enumerated items in a named category ("示范中心" vs ordinary labs).

Only patterns that are unambiguous in Chinese are used: a finding means the two
texts state conditions that cannot both hold, so blocking is safe. Missing or
ambiguous wording yields no finding, and the semantic check stays authoritative.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from campusguide.verification.aggregates import check_aggregate_level
from campusguide.verification.entities import category_violations

MINUTE_UNITS = {"分钟": "分钟"}
UNIT_ALIASES = {
    "分钟": "分钟",
    "小时": "小时",
    "天": "天",
    "日": "天",
    "工作日": "工作日",
    "自然日": "自然日",
    "周": "周",
    "星期": "周",
    "月": "月",
    "学期": "学期",
    "学年": "学年",
    "年": "年",
}

_PREFIX_REL = {
    "内": "within",
    "以内": "within",
    "之内": "within",
    "前": "before",
    "以前": "before",
    "之前": "before",
    "后": "after",
    "之后": "after",
}
_SUFFIX_REL = {
    "后": "after",
    "之后": "after",
    "以后": "after",
    "内": "within",
    "以内": "within",
    "之内": "within",
    "前": "before",
    "以前": "before",
    "之前": "before",
    "以上": "at_least",
    "及以上": "at_least",
    "以下": "at_most",
    "及以下": "at_most",
}

_CN_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100}

_THRESHOLD_RE = re.compile(
    r"(?P<pre>以内|之内|以前|之前|之后|内|前|后)?\s*"
    r"(?P<num>\d+(?:\.\d+)?|[〇零一二三四五六七八九十百两]+)\s*"
    r"(?P<unit>个工作日|个自然日|工作日|自然日|分钟|小时|天|日|周|星期|个?月|学期|学年|年)\s*"
    r"(?P<suf>及以下|及以上|以下|以上|以内|之内|以后|之后|以前|之前|内|前|后)?"
)
_ANCHOR_RE = re.compile(r"([\u4e00-\u9fa5]{2,8})\s*$")

# Opposite directions for the same anchor and unit; "within" and "at_most" agree,
# "after" and "at_least" agree, so only genuinely incompatible pairs are listed.
_INCOMPATIBLE = {
    frozenset({"within", "after"}),
    frozenset({"before", "after"}),
    frozenset({"at_least", "at_most"}),
}

_MODALITIES = {
    "forbid": ("不得", "禁止", "严禁", "不准", "不允许", "不可以", "不可", "不能"),
    "require": ("应当", "必须", "须", "需要", "应"),
    "allow": ("可以", "允许", "能够", "可"),
}
_MODALITY_CLASS = {
    word: klass for klass, words in _MODALITIES.items() for word in words
}
_CLAUSE_SPLIT_RE = re.compile(r"[，。；！？;,.!?\n]")

ROLE_PAIRS = (
    ("第一导师", "第二导师"),
    ("校内导师", "校外导师"),
    ("第一作者", "通讯作者"),
    ("第一完成人", "第二完成人"),
)

_CATEGORY_HEADS = (
    "包括",
    "包含",
    "分为",
    "分别是",
    "有",
    "为",
)
_CATEGORY_SUFFIXES = (
    "中心",
    "实验室",
    "专业",
    "课程",
    "委员会",
    "奖学金",
    "助学金",
    "计划",
    "基地",
    "平台",
    "类别",
    "等级",
    "模块",
    "方向",
    "组织",
    "部门",
    "奖项",
    "项目",
)
_ITEM_SPLIT_RE = re.compile(r"[、,，及和与]")
# PDF extraction wraps lines mid-clause ("不超过\n185 学分", "文科类专业\n（含……）").
# A line break is layout whitespace, not a logical branch delimiter: real branches
# end at 。/；/;, while newline-only enumerations are bounded by their repeated
# category heads in _category_groups.
_BRANCH_SPLIT_RE = re.compile(r"[。；;]")
_COMMA_RE = re.compile(r"[,，]")
_CREDIT_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*学分")
_YEAR_SYSTEM_RE = re.compile(
    r"(?P<num>[0-9]+|[〇零一二三四五六七八九十百两]+)\s*年制"
)
_PARENTHETICAL_RE = re.compile(r"[（(][^）)]*[）)]")

# Two thresholds are only comparable when they regulate the same action; otherwise
# "30分钟后禁止入场" and "30分钟内不得离场" would look like a contradiction.
_ACTION_VERBS = (
    "入场",
    "进场",
    "离场",
    "离开考场",
    "进入考场",
    "候考",
    "提交",
    "报送",
    "递交",
    "办理",
    "审批",
    "审核",
    "公示",
    "公布",
    "报名",
    "申请",
    "报到",
    "注册",
    "答辩",
    "选课",
    "退课",
    "补选",
    "缴费",
    "请假",
    "休学",
    "复学",
    "退学",
    "转专业",
    "评教",
    "填报",
    "签署",
    "备案",
)
_CONTEXT_STOP_RE = re.compile(r"[。；;\n]")

# 同一阈值只有属于同一分支时才可比。分支由“由教务处审批”这类经办主体
# 绑定标识；多分支规则的相邻阈值不能跨分支比较。
_BINDING_VERBS = (
    "审批",
    "审核",
    "批准",
    "审定",
    "办理",
    "负责",
    "决定",
    "同意",
    "受理",
    "备案",
    "登记",
    "处理",
    "公示",
    "公布",
    "核准",
    "出具",
    "开具",
)
_BINDING_VERB_PATTERN = "|".join(_BINDING_VERBS)
_BINDING_RE = re.compile(
    r"(?:报送|报请|须经|需经|应经|由|经|报)"
    r"(?P<actor>[\u4e00-\u9fa5A-Za-z0-9]{1,12}?)"
    rf"(?:{_BINDING_VERB_PATTERN})"
)
_ACTOR_RE = re.compile(
    rf"(?P<actor>[\u4e00-\u9fa5]{{1,10}}?"
    r"(?:部门|办公室|委员会|学院|中心|处|院|系|科|室))"
    rf"(?:{_BINDING_VERB_PATTERN})"
)


@dataclass(frozen=True)
class Threshold:
    anchor: str
    value: float
    unit: str
    relation: str
    span: str
    phase: str = ""
    context: str = ""
    binding: str = ""


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str
    claim_span: str = ""
    quote_span: str = ""
    # True when the claim contradicts the source rather than merely omitting a
    # detail; blocking findings must be revised or dropped (报告 A3).
    blocking: bool = True


def _parse_number(token: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    total = 0
    section = 0
    number = 0
    for char in token:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return None
    return float(total + section + number)


def _anchor(text: str, start: int) -> str:
    head = text[:start]
    head = re.sub(r"(?:当|在|从|于|对|向|自|按照|根据)$", "", head.strip())
    match = _ANCHOR_RE.search(head)
    return match.group(1) if match else ""


def _relation(prefix: str | None, suffix: str | None) -> str | None:
    pre = _PREFIX_REL.get(prefix or "")
    suf = _SUFFIX_REL.get(suffix or "")
    # "开考后30分钟内" and "开学前两周内" both describe a window; the suffix wins.
    if suf == "within":
        return "within"
    if pre == "within":
        return "within"
    if pre == "before":
        return "before"
    if pre == "after":
        return "after"
    if suf:
        return suf
    return None


def _binding(context: str) -> str:
    match = _BINDING_RE.search(context) or _ACTOR_RE.search(context)
    if match is None:
        return ""
    actor = re.sub(r"^(?:报送|报请|须经|需经|应经|由|经|报)", "", match.group("actor"))
    return actor.strip().casefold()


def _inside_parenthetical(text: str, position: int) -> bool:
    head = text[:position]
    opening = max(head.rfind("（"), head.rfind("("))
    closing = max(head.rfind("）"), head.rfind(")"))
    return opening > closing


def _branch_starts(text: str, matches: list[re.Match[str]]) -> list[int]:
    """Real branch starts; an unqualified number inside parentheses is appositive."""
    return [
        match.start()
        for match in matches
        if match.group("pre")
        or match.group("suf")
        or not _inside_parenthetical(text, match.start())
    ]


def extract_thresholds(text: str) -> list[Threshold]:
    found: list[Threshold] = []
    matches = list(_THRESHOLD_RE.finditer(text))
    starts = _branch_starts(text, matches)
    # 相邻起点成对：上下文止于下一个真实分支，括号内复述的同一数字不算分支。
    next_start = dict(itertools.pairwise(starts))
    for match in matches:
        prefix, suffix = match.group("pre"), match.group("suf")
        if not prefix and not suffix:
            continue
        relation = _relation(prefix, suffix)
        if relation is None:
            continue
        value = _parse_number(match.group("num"))
        if value is None:
            continue
        unit = UNIT_ALIASES.get(match.group("unit").lstrip("个"), "")
        if not unit:
            continue
        anchor = _anchor(text, match.start())
        phase = {"before": "before", "after": "after"}.get(
            _PREFIX_REL.get(prefix or ""), ""
        )
        # 上下文止于句读或下一个真实分支，避免把相邻分支的主体/动作带入本分支。
        tail_end = next_start.get(match.start(), len(text))
        tail = text[match.end() : min(match.end() + 60, tail_end)]
        stop = _CONTEXT_STOP_RE.search(tail)
        context = match.group(0) + (tail[: stop.start()] if stop else tail)
        found.append(
            Threshold(
                anchor=anchor,
                value=value,
                unit=unit,
                relation=relation,
                span=match.group(0).strip(),
                phase=phase,
                context=context,
                binding=_binding(context),
            )
        )
    return found


def _actions(tokens: str) -> set[str]:
    return {verb for verb in _ACTION_VERBS if verb in tokens}


def _same_action(left: Threshold, right: Threshold) -> bool:
    shared = _actions(left.context) & _actions(right.context)
    return bool(shared)


def _anchors_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if left.endswith(right) or right.endswith(left):
        return len(right) >= 2 and len(left) >= 2
    return False


def _bindings_match(left: str, right: str) -> bool:
    if not left or not right:
        return left == right
    if left == right:
        return True
    return len(left) >= 2 and len(right) >= 2 and (
        left.endswith(right) or right.endswith(left)
    )


def _branches_align(claim: Threshold, quote: Threshold) -> bool:
    """Only thresholds bound to the same actor/result branch are comparable.

    A one-sided binding is ambiguous (the other text may have paraphrased the
    actor away), so it stays with the semantic checker instead of producing a
    deterministic contradiction.
    """
    return _bindings_match(claim.binding, quote.binding)


def _thresholds_comparable(claim: Threshold, quote: Threshold) -> bool:
    return (
        _branches_align(claim, quote)
        and claim.unit == quote.unit
        and _anchors_match(claim.anchor, quote.anchor)
        and _same_action(claim, quote)
    )


def _thresholds_exact(claim: Threshold, quote: Threshold) -> bool:
    """The same assertion inside the same branch, not merely an equal number."""
    if claim.value != quote.value:
        return False
    if claim.phase and quote.phase and claim.phase != quote.phase:
        return False
    return frozenset({claim.relation, quote.relation}) not in _INCOMPATIBLE


def compare_thresholds(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    claim_items = extract_thresholds(claim_text)
    if not claim_items:
        return []
    quote_items = [item for text in quote_texts for item in extract_thresholds(text)]
    findings: list[Finding] = []
    for claim in claim_items:
        aligned = [item for item in quote_items if _thresholds_comparable(claim, item)]
        # 同一分支已有一致断言时，其他分支的阈值不能再用笛卡尔配对否定它。
        if any(_thresholds_exact(claim, item) for item in aligned):
            continue
        for quote in aligned:
            if claim.value != quote.value:
                if {claim.relation, quote.relation} & {"after", "within", "before"}:
                    findings.append(
                        Finding(
                            kind="threshold_value",
                            detail=(
                                f"同一条件（{claim.anchor}）在原文为 {quote.value:g}"
                                f"{quote.unit}，结论写成 {claim.value:g}{claim.unit}"
                            ),
                            claim_span=claim.span,
                            quote_span=quote.span,
                        )
                    )
                continue
            if claim.phase and quote.phase and claim.phase != quote.phase:
                findings.append(
                    Finding(
                        kind="threshold_phase",
                        detail=(
                            f"时间基准相反：原文“{quote.span}”指向"
                            f"{'开始前' if quote.phase == 'before' else '开始后'}，"
                            f"结论“{claim.span}”指向"
                            f"{'开始前' if claim.phase == 'before' else '开始后'}"
                        ),
                        claim_span=claim.span,
                        quote_span=quote.span,
                    )
                )
                continue
            if frozenset({claim.relation, quote.relation}) in _INCOMPATIBLE:
                findings.append(
                    Finding(
                        kind="threshold_direction",
                        detail=(
                            f"条件方向相反：原文“{quote.span}”（{quote.relation}），"
                            f"结论“{claim.span}”（{claim.relation}）"
                        ),
                        claim_span=claim.span,
                        quote_span=quote.span,
                    )
                )
    return findings


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]


def _modality(text: str) -> tuple[str, str] | None:
    for word in sorted(_MODALITY_CLASS, key=len, reverse=True):
        if word in text:
            action = text.replace(word, "", 1)
            action = re.sub(r"^(?:学生|考生|研究生|本科生|学校|学院|导师|教师)", "", action)
            return _MODALITY_CLASS[word], action.strip()
    return None


def compare_modalities(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for claim_clause in _clauses(claim_text):
        parsed = _modality(claim_clause)
        if parsed is None:
            continue
        claim_class, claim_action = parsed
        if len(claim_action) < 6:
            continue
        for quote in quote_texts:
            for quote_clause in _clauses(quote):
                parsed_quote = _modality(quote_clause)
                if parsed_quote is None:
                    continue
                quote_class, quote_action = parsed_quote
                if {claim_class, quote_class} != {"forbid", "allow"}:
                    continue
                if claim_action not in quote_action and quote_action not in claim_action:
                    continue
                findings.append(
                    Finding(
                        kind="modality",
                        detail=(
                            f"义务方向相反：原文“{quote_clause[:40]}”为"
                            f"{'禁止' if quote_class == 'forbid' else '允许'}，"
                            f"结论“{claim_clause[:40]}”为"
                            f"{'禁止' if claim_class == 'forbid' else '允许'}"
                        ),
                        claim_span=claim_clause,
                        quote_span=quote_clause,
                    )
                )
    return findings


def _role_verbs(text: str, role: str) -> set[str]:
    verbs: set[str] = set()
    for match in re.finditer(re.escape(role), text):
        tail = text[match.end() : match.end() + 6]
        tail = re.sub(r"^(?:是|为|由|应当|须|必须|可以|负责|担任|作为)", "", tail)
        verbs.add(tail[:3].strip("的，。；、：:（）()"))
    return {verb for verb in verbs if verb}


def compare_roles(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    joined_quote = "\n".join(quote_texts)
    for left, right in ROLE_PAIRS:
        claim_left = _role_verbs(claim_text, left)
        claim_right = _role_verbs(claim_text, right)
        if not claim_left and not claim_right:
            continue
        quote_left = _role_verbs(joined_quote, left)
        quote_right = _role_verbs(joined_quote, right)
        # A claim that assigns left-role behaviour to the right role (or the
        # reverse) while the quote assigns it to the other one is inconsistent.
        if claim_right & quote_left and not claim_left:
            findings.append(
                Finding(
                    kind="role",
                    detail=f"角色对调：原文把该职责归于{left}，结论写成{right}",
                    claim_span=right,
                    quote_span=left,
                )
            )
        if claim_left & quote_right and not claim_right:
            findings.append(
                Finding(
                    kind="role",
                    detail=f"角色对调：原文把该职责归于{right}，结论写成{left}",
                    claim_span=left,
                    quote_span=right,
                )
            )
    return findings


def _split_items(raw: str) -> list[str]:
    items = []
    for part in _ITEM_SPLIT_RE.split(raw):
        item = part.strip(" 　。；;：:")
        item = re.sub(r"^(?:等|以及|还有|其中)", "", item)
        if 2 <= len(item) <= 24:
            items.append(item)
    return items


def _clean_category(raw: str) -> str:
    category = re.sub(
        r"^(?:学院|学校|其中|目前|现有|共|设|设有|开设)", "", raw.strip()
    ).strip()
    if len(category) < 3 or not category.endswith(_CATEGORY_SUFFIXES):
        return ""
    return category


def _category_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    return (
        len(left) >= 3
        and len(right) >= 3
        and (left.endswith(right) or right.endswith(left))
    )


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _item_key(text: str) -> str:
    compact = _PARENTHETICAL_RE.sub("", _compact_text(text))
    return compact.rstrip("的")


def _year_systems(text: str) -> list[str]:
    found: list[str] = []
    for match in _YEAR_SYSTEM_RE.finditer(text):
        value = _parse_number(match.group("num"))
        if value is None:
            continue
        normalized = f"{value:g}年制"
        if normalized not in found:
            found.append(normalized)
    return found


def _branch_credit(text: str) -> float | None:
    values = {float(match.group("num")) for match in _CREDIT_RE.finditer(text)}
    return next(iter(values)) if len(values) == 1 else None


@dataclass(frozen=True)
class _Branch:
    start: int
    end: int
    text: str
    year_system: str = ""
    credit: float | None = None


@dataclass(frozen=True)
class _CategoryGroup:
    category: str
    items_text: str
    start: int
    end: int
    year_system: str = ""
    credit: float | None = None


def _branches(text: str) -> list[_Branch]:
    """Split at logical branch punctuation, carrying a year-system qualifier across ；.

    Line breaks stay inside the branch: they are usually PDF layout wraps, and
    newline-only enumerations are separated by their category heads below.
    """
    branches: list[_Branch] = []
    year_system = ""
    start = 0
    boundaries = [match.start() for match in _BRANCH_SPLIT_RE.finditer(text)]
    boundaries.append(len(text))
    for end in boundaries:
        segment = text[start:end]
        own_years = _year_systems(segment)
        if own_years:
            year_system = own_years[0] if len(own_years) == 1 else ""
        branches.append(
            _Branch(
                start=start,
                end=end,
                text=segment,
                year_system=year_system,
                credit=_branch_credit(segment),
            )
        )
        if end < len(text) and text[end] == "。":
            year_system = ""
        start = end + 1
    return branches


def _category_candidates(text: str) -> list[tuple[str, str, int, int]]:
    """Every valid ``category + head`` occurrence, including repeated branches."""
    raw: list[tuple[str, str, int, int]] = []
    for match in re.finditer(r"(?P<cat>[^，,。；;：:\n]{2,20})[：:]", text):
        raw.append((match.group("cat"), "：", match.start("cat"), match.end()))
    for head in _CATEGORY_HEADS:
        pattern = r"(?P<cat>[^，,。；;：:\n]{2,20}?)" + re.escape(head)
        for match in re.finditer(pattern, text):
            raw.append((match.group("cat"), head, match.start("cat"), match.end()))
    candidates: list[tuple[str, str, int, int]] = []
    for raw_category, head, start, items_start in raw:
        category = _clean_category(raw_category)
        if category:
            candidates.append((category, head, start, items_start))
    candidates.sort(key=lambda candidate: (candidate[2], candidate[3]))
    unique: dict[tuple[str, str, int, int], tuple[str, str, int, int]] = {}
    for candidate in candidates:
        unique.setdefault(candidate, candidate)
    return list(unique.values())


def _category_groups(text: str) -> list[_CategoryGroup]:
    """Category enumerations bounded to their own branch, with year/credit binding.

    An occurrence must not swallow a following ``第二主修专业为……`` branch, and
    the consequence clause after an appositive enumeration (``……的，培养方案……``)
    is not an item of that enumeration.
    """
    groups: list[_CategoryGroup] = []
    for branch in _branches(text):
        candidates = _category_candidates(branch.text)
        for index, (category, head, start, items_start) in enumerate(candidates):
            limit = len(branch.text)
            later = [
                other_start
                for _, _, other_start, _ in candidates[index + 1 :]
                if other_start > items_start
            ]
            if later:
                limit = min(later)
            region_end = limit
            if head == "为":
                comma = _COMMA_RE.search(branch.text, items_start, limit)
                if comma is not None:
                    intro = branch.text[items_start : comma.start()].strip()
                    if intro.endswith("的") or "、" in intro:
                        limit = comma.start()
            items_text = branch.text[items_start:limit].strip(" \n　，,。；;：:")
            if len(items_text) < 2:
                continue
            # Bind year/credit to this category head's own region, not the whole
            # branch, so newline-only enumerations do not share one consequence.
            region = branch.text[start:region_end]
            own_years = _year_systems(region)
            groups.append(
                _CategoryGroup(
                    category=category,
                    items_text=items_text,
                    start=branch.start + start,
                    end=branch.start + limit,
                    year_system=(
                        own_years[0]
                        if len(own_years) == 1
                        else branch.year_system
                    ),
                    credit=_branch_credit(region),
                )
            )
    unique: dict[tuple[int, str, str], _CategoryGroup] = {}
    for group in groups:
        unique.setdefault((group.start, group.category, group.items_text), group)
    return list(unique.values())


def _category_overlap(items: list[str], group: _CategoryGroup) -> int:
    group_key = _item_key(group.items_text)
    keys = [_item_key(item) for item in items]
    return sum(1 for key in keys if key and key in group_key)


def _outside_items(
    items: list[str], group: _CategoryGroup, quote: str
) -> list[str]:
    group_key = _item_key(group.items_text)
    quote_key = _item_key(quote)
    return [
        item
        for item in items
        if _item_key(item) not in group_key and _item_key(item) in quote_key
    ]


def _category_binding_notes(
    claim: _CategoryGroup, quote: _CategoryGroup
) -> list[str]:
    notes: list[str] = []
    if (
        claim.year_system
        and quote.year_system
        and claim.year_system != quote.year_system
    ):
        notes.append(
            f"原文对应分支为{quote.year_system}，结论写成{claim.year_system}"
        )
    if (
        claim.credit is not None
        and quote.credit is not None
        and claim.credit != quote.credit
    ):
        notes.append(
            f"原文同一分支为{quote.credit:g}学分，结论写成{claim.credit:g}学分"
        )
    return notes


def _category_finding(
    claim: _CategoryGroup,
    quote: _CategoryGroup,
    outside: list[str],
    binding_notes: list[str],
) -> Finding:
    parts = [f"{claim.category}：{note}" for note in binding_notes]
    if outside:
        parts.append(
            f"{claim.category}在原文同一列举中只到"
            f"“{quote.items_text[:50].strip()}”，"
            f"结论把{'、'.join(outside[:3])}也归入该类"
        )
    return Finding(
        kind="category",
        detail="实体类别越界：" + "；".join(parts),
        claim_span=claim.items_text[:60],
        quote_span=quote.items_text[:60],
    )


def _foreign_category_finding(
    claim: _CategoryGroup, outside: list[str], quote: str
) -> Finding:
    return Finding(
        kind="category",
        detail=(
            f"实体类别越界：原文未把{'、'.join(outside[:3])}归入"
            f"{claim.category}，该列举出现在原文其他类别或分支中"
        ),
        claim_span=claim.items_text[:60],
        quote_span=quote[:60],
    )


def _compare_category_group(
    claim_group: _CategoryGroup, claim_items: list[str], quote: str
) -> Finding | None:
    all_groups = _category_groups(quote)
    quote_groups = [
        group
        for group in all_groups
        if _category_matches(group.category, claim_group.category)
    ]
    candidates: list[tuple[int, bool, bool, _CategoryGroup]] = []
    for group in quote_groups:
        candidates.append(
            (
                _category_overlap(claim_items, group),
                not (
                    claim_group.year_system
                    and group.year_system
                    and claim_group.year_system != group.year_system
                ),
                not (
                    claim_group.credit is not None
                    and group.credit is not None
                    and claim_group.credit != group.credit
                ),
                group,
            )
        )
    exact = [
        candidate
        for candidate in candidates
        if candidate[0] and candidate[1] and candidate[2]
    ]
    if exact:
        best = max(exact, key=lambda candidate: candidate[0])[3]
        outside = _outside_items(claim_items, best, quote)
        if outside:
            return _category_finding(claim_group, best, outside, [])
        return None
    overlapping = [candidate for candidate in candidates if candidate[0]]
    if overlapping:
        best = max(
            overlapping,
            key=lambda candidate: (candidate[0], candidate[1], candidate[2]),
        )[3]
        outside = _outside_items(claim_items, best, quote)
        notes = _category_binding_notes(claim_group, best)
        if outside or notes:
            return _category_finding(claim_group, best, outside, notes)
        return None
    outside = [
        item
        for item in claim_items
        if _item_key(item) in _item_key(quote)
        and not any(
            _item_key(item) in _item_key(group.items_text) for group in quote_groups
        )
    ]
    misfiled = [
        item
        for item in outside
        if any(
            not _category_matches(group.category, claim_group.category)
            and _item_key(item) in _item_key(group.items_text)
            for group in all_groups
        )
    ]
    if misfiled:
        return _foreign_category_finding(claim_group, misfiled, quote)
    return None


def compare_categories(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    """Reject list items that inherit a category they are not filed under."""
    findings: list[Finding] = []
    # 098：真实语料是“三个示范中心+十余个普通实验室”的连续无标点列举，
    # 先按每个实体自身的类型后缀逐项判别，再退回分段规则。
    for quote in quote_texts:
        violations = category_violations(claim_text, quote)
        if violations:
            findings.append(
                Finding(
                    kind="category",
                    detail=(
                        "实体类别越界：原文“实验教学示范中心”只包括三个中心，"
                        f"结论把{'、'.join(violations[:3])}也归入该类"
                    ),
                    claim_span=claim_text[:60],
                    quote_span=quote[:60],
                )
            )
            return findings
    for claim_group in _category_groups(claim_text):
        items = _split_items(claim_group.items_text)
        if len(items) < 2:
            continue
        for quote in quote_texts:
            finding = _compare_category_group(claim_group, items, quote)
            if finding is not None:
                findings.append(finding)
                break
    unique: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.kind, finding.detail), finding)
    return list(unique.values())


_EXCEPTION_RE = re.compile(
    r"(?P<marker>除外|不适用|另按|另行规定|例外)"
    r"|除(?P<prefix>[^，。；;\n]{2,30}?)外"
)


def _quadgrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[index : index + 4] for index in range(max(0, len(compact) - 3))}


_BRANCH_RE = re.compile(
    r"(?:（[一二三四五六七八九十]）|\([一二三四五六七八九十]\)|第[一二三四五六七八九十]+条)?\s*"
    r"(?P<cond>[^。；;\n（(]{4,40}?的情形)\s*[。，；;]?\s*[^。；;\n]{0,40}?"
    r"署名单位(?P<modal>必须为|须为|应为|可为|可以是|为)"
    r"(?P<units>[^。；;\n]{2,60})"
)
_UNIVERSAL_RE = re.compile(r"所有|全部|任何|一律|均须|均需|都必须|均|统一")
_CONDITION_HINTS = ("我校", "联合培养单位", "本校", "合作单位")
_UNIT_SPLIT_RE = re.compile(r"或者|或|、|以及|和")


@dataclass(frozen=True)
class SignatureBranch:
    condition: str
    modal: str
    units: tuple[str, ...]

    @property
    def exclusive(self) -> bool:
        return self.modal in {"必须为", "须为", "应为"}


def signature_branches(text: str) -> list[SignatureBranch]:
    """Branch rules such as “第一导师为X的情形……署名单位必须为Y”。

    Real 035 supplement never says “除外” or “例外”; the exception is encoded as a
    condition on the first supervisor's affiliation.
    """
    branches: list[SignatureBranch] = []
    for match in _BRANCH_RE.finditer(text):
        units = tuple(
            unit.strip(" “”\"'。；;")
            for unit in _UNIT_SPLIT_RE.split(match.group("units"))
            if len(unit.strip(" “”\"'。；;")) >= 2
        )
        if not units:
            continue
        branches.append(
            SignatureBranch(
                condition=match.group("cond").strip(),
                modal=match.group("modal"),
                units=units,
            )
        )
    return branches


def _claim_units(claim_text: str) -> tuple[str, ...]:
    patterns = (
        r"署名单位[^。；]{0,60}?(?:必须为|须为|应为|可为|可以是|为)(?P<units>[^。；]{2,60})",
        r"(?:必须|须|应|可|均须|均需)?\s*(?:以|为)\s*(?P<units>[^，。；]{2,40}?)\s*"
        r"(?:为|作为)\s*(?:其)?第一署名单位",
        r"(?P<units>[^，。；]{2,40}?)\s*为\s*(?:其)?第一署名单位",
    )
    for pattern in patterns:
        match = re.search(pattern, claim_text)
        if match is None:
            continue
        units = tuple(
            unit.strip(" “”\"'。；;")
            for unit in _UNIT_SPLIT_RE.split(match.group("units"))
            if len(unit.strip(" “”\"'。；;")) >= 2
        )
        if units:
            return units
    return ()


def _is_universal(claim_text: str) -> bool:
    """A rule stated for a whole group without its applicability condition."""
    if _UNIVERSAL_RE.search(claim_text):
        return True
    group = re.search(r"研究生|本科生|联培|学生|教师", claim_text)
    modal = re.search(r"必须|须|应当|应|一律|均", claim_text)
    return bool(group and modal)


def _condition_of(claim_text: str) -> str | None:
    for hint in _CONDITION_HINTS:
        if hint in claim_text:
            return hint
    return None


def compare_signature_branches(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    """A universal signature rule must not override a conditional branch (035)."""
    claim_units = _claim_units(claim_text)
    if not claim_units:
        return []
    findings: list[Finding] = []
    for quote in quote_texts:
        branches = signature_branches(quote)
        if len(branches) < 2:
            continue
        allowed_all = [set(branch.units) for branch in branches]
        condition = _condition_of(claim_text)
        if condition is not None:
            matched = [b for b in branches if condition in b.condition]
            if not matched:
                continue
            branch = matched[0]
            if not set(claim_units) <= set(branch.units):
                findings.append(
                    Finding(
                        kind="exception_dropped",
                        detail=(
                            f"条件分支不符：原文“{branch.condition}”只允许"
                            f"{'或'.join(branch.units)}，结论写成{'、'.join(claim_units)}"
                        ),
                        claim_span=claim_text[:60],
                        quote_span=branch.condition,
                        blocking=True,
                    )
                )
            continue
        # No condition stated: a universal claim must hold in every branch.
        if not _is_universal(claim_text):
            continue
        exclusive = bool(re.search(r"必须|须|应当|应|一律|均", claim_text))
        conflicts = [
            branch
            for branch, allowed in zip(branches, allowed_all, strict=True)
            # 全称“必须为X”还会排除分支允许的其他选项（035 双单位认可制）。
            if not set(claim_units) <= allowed
            or (exclusive and not allowed <= set(claim_units))
        ]
        if conflicts:
            branch = conflicts[0]
            findings.append(
                Finding(
                    kind="exception_dropped",
                    detail=(
                        "原文按“第一导师”归属分为不同情形，其中"
                        f"“{branch.condition}”允许{'或'.join(branch.units)}；"
                        f"结论未说明适用条件，写成面向全体研究生的全称规则"
                    ),
                    claim_span=claim_text[:60],
                    quote_span=branch.condition,
                    blocking=True,
                )
            )
    return findings


def compare_exceptions(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    """A condition attached to a named group must not become a general rule.

    Two levels, matching 报告 A3:
    * branch-structured exceptions (035 双单位认可制) are blocking when the claim
      states the opposite universal rule;
    * marker-word exceptions (除外/不适用) stay advisory, because a summary may
      legitimately omit a rarely relevant exception.
    """
    findings = compare_signature_branches(claim_text, quote_texts)
    if findings:
        return findings
    if re.search(r"除外|除.{0,20}外|不适用|另行|例外|特殊", claim_text):
        return []
    claim_grams = _quadgrams(claim_text)
    for quote in quote_texts:
        match = _EXCEPTION_RE.search(quote)
        if match is None:
            continue
        if len(claim_grams & _quadgrams(quote)) < 3:
            continue
        window = quote[max(0, match.start() - 40) : match.start()]
        clause = re.split(r"[，。；;\n]", window)[-1].strip()
        body = (match.group("prefix") or "") or clause
        findings.append(
            Finding(
                kind="exception_dropped",
                detail=(
                    f"原文对该情形有例外（“{body[:20]}…{match.group(0)[:12]}”），"
                    "结论写成通用规则，需保留适用条件或注明例外"
                ),
                claim_span=claim_text[:60],
                quote_span=match.group(0)[:60],
                blocking=False,
            )
        )
    return findings


def check_conditions(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    """All deterministic condition findings for one claim against its quotes."""
    if not claim_text or not quote_texts:
        return []
    findings: list[Finding] = []
    findings.extend(compare_thresholds(claim_text, quote_texts))
    findings.extend(compare_modalities(claim_text, quote_texts))
    findings.extend(compare_roles(claim_text, quote_texts))
    findings.extend(compare_categories(claim_text, quote_texts))
    findings.extend(compare_exceptions(claim_text, quote_texts))
    findings.extend(
        Finding(kind=item.kind, detail=item.detail)
        for item in check_aggregate_level(claim_text, quote_texts)
    )
    unique: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.kind, finding.detail), finding)
    return list(unique.values())


__all__ = [
    "Finding",
    "Threshold",
    "check_conditions",
    "compare_categories",
    "compare_modalities",
    "compare_roles",
    "compare_thresholds",
    "extract_thresholds",
]
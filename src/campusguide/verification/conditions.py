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
_SEGMENT_STOP_RE = re.compile(r"[。；;\n]|另设|另有|此外|其他|其余|以及|还有")
_ITEM_SPLIT_RE = re.compile(r"[、,，及和与]")

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


@dataclass(frozen=True)
class Threshold:
    anchor: str
    value: float
    unit: str
    relation: str
    span: str
    phase: str = ""
    context: str = ""


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


def extract_thresholds(text: str) -> list[Threshold]:
    found: list[Threshold] = []
    for match in _THRESHOLD_RE.finditer(text):
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
        tail = text[match.end() : match.end() + 60]
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


def compare_thresholds(claim_text: str, quote_texts: list[str]) -> list[Finding]:
    claim_items = extract_thresholds(claim_text)
    if not claim_items:
        return []
    quote_items = [item for text in quote_texts for item in extract_thresholds(text)]
    findings: list[Finding] = []
    for claim in claim_items:
        for quote in quote_items:
            if claim.unit != quote.unit or not _anchors_match(claim.anchor, quote.anchor):
                continue
            if not _same_action(claim, quote):
                continue
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


def _category_groups(text: str) -> list[tuple[str, str]]:
    """(category, item text) pairs from "C：a、b、c" or "C包括a、b"."""
    groups: list[tuple[str, str]] = []
    for separator in ("：", ":"):
        for match in re.finditer(r"(?P<cat>[^，。；：:\n]{2,20})" + separator + r"\s*(?P<items>[^。；;\n]{4,160})", text):
            groups.append((match.group("cat").strip(), match.group("items")))
    for head in _CATEGORY_HEADS:
        pattern = r"(?P<cat>[^，。；：:\n]{2,20}?)" + head + r"\s*(?P<items>[^。；;\n]{4,160})"
        for match in re.finditer(pattern, text):
            groups.append((match.group("cat").strip(), match.group("items")))
    cleaned: list[tuple[str, str]] = []
    for category, items in groups:
        category = re.sub(r"^(?:学院|学校|其中|目前|现有|共|设|设有|开设)", "", category).strip()
        if len(category) < 3 or not category.endswith(_CATEGORY_SUFFIXES):
            continue
        cleaned.append((category, items))
    return cleaned


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
    for category, raw_items in _category_groups(claim_text):
        items = _split_items(raw_items)
        if len(items) < 2:
            continue
        for quote in quote_texts:
            position = quote.find(category)
            if position < 0:
                continue
            tail = quote[position + len(category) :]
            stop = _SEGMENT_STOP_RE.search(tail)
            segment = tail[: stop.start()] if stop else tail[:200]
            outside = [item for item in items if item not in segment and item in tail]
            if outside:
                findings.append(
                    Finding(
                        kind="category",
                        detail=(
                            f"实体类别越界：{category}在原文同一列举中只到"
                            f"“{segment[:50].strip()}”，结论把{'、'.join(outside[:3])}也归入该类"
                        ),
                        claim_span=raw_items[:60],
                        quote_span=segment[:60],
                    )
                )
                break
    return findings


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
            for branch, allowed in zip(branches, allowed_all)
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
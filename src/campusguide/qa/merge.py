"""Merge verified claims before rendering (R6 / 报告 A1、A6).

Rules the report demands:

* the claim key carries object, attribute, level, class type, cohort/version and
  applicability condition, so two correct facts for different cohorts are never
  collapsed;
* near-duplicates only collapse when numbers, negation, time and scope agree;
* a real conflict is only raised when two statements bind the same subject,
  property, scope and condition and actually disagree on a number, role, value,
  polarity or modality; paraphrases, subsets and complementary facts are not
  conflicts, and an unresolved relation keeps both statements without a note;
* a conflict is never resolved by citation count or input order: both
  statements are listed with a "verify" note;
* a "未收录/未提及" statement is retracted whenever another claim covers the same
  item, regardless of order;
* notes are typed (duplicate / conflict / stale_missing) so only substantive
  gaps can affect completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from campusguide.domain.models import Citation, Claim
from campusguide.verification.facts import extract_facts

_TABLE_LINE = re.compile(r"^\s*(?:[|｜].*[|｜]|[-—–]{3,}|\d+(?:\.\d+)?\s*[|｜].*)\s*$")
_SPACE = re.compile(r"\s+")
_NEGATIVE = re.compile(
    r"未收录|未提及|未找到|未检索到|没有检索到|暂无相关|没有相关规定|尚未收录|证据未提及|"
    r"未被收录|未明确|尚缺|没有提及|未包含|未列出|未说明|未注明|未公布"
)
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_COHORT = re.compile(r"20\d{2}\s*(?:级|版|届)")
_RANGE = re.compile(r"20\d{2}\s*[-—~至]\s*20\d{2}\s*(?:级|版|届)?")
_LEVEL = re.compile(r"博士研究生|硕士研究生|研究生|本科生|本科|博士|硕士")
_CLASS = re.compile(r"丁颖创新班|创新班|实验班|卓越班|普通班|辅修学士学位|辅修专业|微专业")
_CONDITION = re.compile(
    r"第一导师为(?:我校|本校|联合培养单位|合作单位)[^，。；]{0,6}|"
    r"联合培养|首次|第一次|超过|未满|满[一二三四五六七八九十\d]+"
)
_CLAUSE_SPLIT = re.compile(r"[，,。；;：:\n]")
_CONDITION_TERM = re.compile(
    r"四年制|五年制|三年制|两年制|"
    r"休学创业|因病休学|因病|因疾病|因出国|因特殊困难|"
    r"第一导师为(?:我校|本校|联合培养单位|合作单位)|联合培养|"
    r"首次|第一次|超过|未满|满[一二三四五六七八九十\d]+|"
    r"全日制|非全日制|学术学位|专业学位|定向|非定向|"
    r"校内|校外|省内|跨省"
)
_FILLER = re.compile(r"自己|本人|该校|相关|有关")
_MODALITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "forbid",
        (
            "不允许",
            "不可以",
            "不需要",
            "不得",
            "禁止",
            "严禁",
            "不准",
            "不可",
            "不能",
            "无需",
            "无须",
            "不用",
        ),
    ),
    ("require", ("应当", "必须", "需要", "须", "应")),
    ("allow", ("可以", "允许", "能够", "可")),
)
_MODALITY_CLASS = {word: name for name, words in _MODALITIES for word in words}
_MODALITY_OR_NEGATION = re.compile(
    "|".join(re.escape(word) for word in sorted(_MODALITY_CLASS, key=len, reverse=True))
    + r"|不超过|未满|未达|未达到"
)
_POLARITY_NEGATIVE = re.compile(
    r"不需要|无需|无须|不用|不得|禁止|严禁|不准|不允许|不可以|不能|不可|不超过|未满|未达"
)
_ROLE_VERBS = (
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
    "组织",
)
_EXPLICIT_ROLE_RE = re.compile(
    r"(?:须经|需经|由|报|送|经)"
    r"(?P<authority>[\u4e00-\u9fa5]{1,24}?)"
    r"(?P<action>" + "|".join(_ROLE_VERBS) + ")"
)
_ROLE_LINK_TAIL = re.compile(r"(?:同时|后|再|并|且)+$")
_ROLE_SUBJECT_STRIP = re.compile(r"[，,。；;：:、\s（）()【】\[\]《》“”‘’]")
_THRESHOLD = re.compile(
    r"(?:超过|不超过|未满|不满|满)?\s*\d+(?:\.\d+)?\s*"
    r"(?:个?月|天|日|周|年|小时)\s*(?:以内|以上|以下|及以上|及以下|之内|之外)?"
)
_VALUE_PROPERTIES = (
    "第一署名单位",
    "署名单位",
    "审批单位",
    "审核单位",
    "负责部门",
    "主管部门",
    "受理单位",
    "办理单位",
    "发放单位",
    "组织单位",
    "认定单位",
    "学位授予单位",
)
_VALUE_ASSERTION = re.compile(
    r"(?P<head>[^，,。；;：:\n]{2,28}?)"
    r"(?P<property>" + "|".join(sorted(_VALUE_PROPERTIES, key=len, reverse=True)) + ")"
    r"(?:必须|须|应当|应|均|一律)?(?:为|是|由)"
    r"(?P<value>[^，,。；;：:\n]{1,30})"
)

ATTRIBUTES: tuple[str, ...] = (
    "署名单位",
    "毕业总学分",
    "学分",
    "学时",
    "课程",
    "金额",
    "比例",
    "期限",
    "证书",
    "学位",
    "学制",
    "条件",
    "程序",
    "材料",
    "时间",
    "地点",
    "职责",
    "名额",
    "成绩",
    "资格",
)


@dataclass(frozen=True)
class MergeNote:
    kind: str  # duplicate | conflict | stale_missing | cross_version | table_fragment
    detail: str


@dataclass
class MergeResult:
    claims: list[Claim] = field(default_factory=list)
    notes: list[MergeNote] = field(default_factory=list)
    dropped_claim_ids: list[str] = field(default_factory=list)
    replacement_claim_ids: dict[str, str] = field(default_factory=dict)

    @property
    def dropped(self) -> int:
        return len(self.dropped_claim_ids)

    @property
    def substantive_notes(self) -> list[MergeNote]:
        """Notes that must be shown to the reader (conflicts need verification)."""
        return [note for note in self.notes if note.kind == "conflict"]

    @property
    def info_notes(self) -> list[MergeNote]:
        return [note for note in self.notes if note.kind != "conflict"]


@dataclass(frozen=True)
class ClaimKey:
    object: str
    attribute: str
    level: str
    class_type: str
    scope: str
    condition: str


def clean_claim_text(text: str) -> str:
    """Drop table fragments and stray separators from a rendered claim."""
    lines = [line for line in text.splitlines() if not _TABLE_LINE.match(line)]
    cleaned = _SPACE.sub(" ", " ".join(lines)).strip()
    cleaned = re.sub(r"^(?:已核实|已确认)[：:]\s*", "", cleaned)
    cleaned = re.sub(r"^[\s、，,；;：:]+|[\s、，,；;：:]+$", "", cleaned)
    return cleaned


def _compact(text: str) -> str:
    return _SPACE.sub("", text)


def _scope(text: str) -> str:
    ranged = _RANGE.search(text)
    if ranged:
        return _SPACE.sub("", ranged.group(0))
    cohort = _COHORT.search(text)
    return _SPACE.sub("", cohort.group(0)) if cohort else ""


def _condition(text: str) -> str:
    match = _CONDITION.search(text)
    return _SPACE.sub("", match.group(0)) if match else ""


def _attribute(text: str) -> str:
    for attribute in ATTRIBUTES:
        if attribute in text:
            return attribute
    return ""


def _object_head(text: str) -> str:
    attribute = _attribute(text)
    head = text[: text.find(attribute)] if attribute else text
    explicit = _explicit_roles(head)
    if explicit:
        head = head[: explicit[0][0].start()]
    head = _RANGE.sub("", head)
    head = _COHORT.sub("", head)
    head = _LEVEL.sub("", head)
    head = _CLASS.sub("", head)
    head = _NUMBER_TOKEN.sub("", head)
    head = _MODALITY_OR_NEGATION.sub("", head)
    head = re.sub(r"[不未]", "", head)
    head = re.sub(r"[，,。；;：:、\s（）()【】\[\]]", "", head)
    return head[:16]


def _key(text: str) -> ClaimKey:
    level = _LEVEL.search(text)
    class_type = _CLASS.search(text)
    return ClaimKey(
        object=_object_head(text),
        attribute=_attribute(text),
        level=level.group(0) if level else "",
        class_type=class_type.group(0) if class_type else "",
        scope=_scope(text),
        condition=_condition(text),
    )


_QUANTIFIERS = re.compile(r"其余|其他|其它|所有|全部|各|均|都|仍未|尚未|没有|无|缺|未")


def _topics(text: str) -> list[str]:
    """Content terms of a negative statement, quantifiers and negation removed."""
    body = _NEGATIVE.sub("", text)
    body = _QUANTIFIERS.sub("", body)
    body = re.sub(r"[，,。；;：:、\s（）()【】\[\]]", "", body)
    if len(body) < 2:
        return []
    topics = [body]
    # also the trailing noun phrase, e.g. “各系主任” -> “系主任”
    for size in (4, 3, 2):
        if len(body) > size:
            topics.append(body[-size:])
    return list(dict.fromkeys(topics))


def _subject(text: str) -> str:
    """Stable subject prefix used to decide whether two claims cover one item."""
    clause = re.split(r"[。；;，,]", text)[0]
    clause = _CLASS.sub("", clause)
    clause = _LEVEL.sub("", clause)
    clause = _NUMBER_TOKEN.sub("", clause)
    clause = _NEGATIVE.sub("", clause)
    return _compact(clause)[:12]


def _modality(text: str) -> str:
    for word in sorted(_MODALITY_CLASS, key=len, reverse=True):
        if word in text:
            return _MODALITY_CLASS[word]
    return ""


def _condition_terms(text: str) -> frozenset[str]:
    return frozenset(
        _SPACE.sub("", match.group(0)) for match in _CONDITION_TERM.finditer(text)
    )


def _canonical(text: str) -> str:
    body = _MODALITY_OR_NEGATION.sub("", text)
    body = _FILLER.sub("", body)
    return re.sub(r"[，,。；;：:、（）()【】\[\]\s]", "", body)


def _content_grams(text: str) -> set[str]:
    compact = _compact(text)
    size = 3 if len(compact) >= 5 else 2
    return {
        compact[index : index + size]
        for index in range(max(0, len(compact) - size + 1))
    }


def _assertion_grams(text: str) -> set[str]:
    return _content_grams(_MODALITY_OR_NEGATION.sub("", text))


def _containment(part: set[str], whole: set[str]) -> float:
    if not part or not whole:
        return 0.0
    return len(part & whole) / len(part)


def _fact_set(text: str) -> frozenset[str]:
    return frozenset(fact.canonical for fact in extract_facts(text))


def _role_subject(text: str, position: int) -> str:
    prefix = _EXPLICIT_ROLE_RE.sub("", text[:position])
    prefix = _ROLE_SUBJECT_STRIP.sub("", prefix)
    return _ROLE_LINK_TAIL.sub("", prefix)


def _explicit_roles(text: str) -> list[tuple[re.Match[str], str]]:
    roles: list[tuple[re.Match[str], str]] = []
    position = 0
    while position < len(text):
        match = _EXPLICIT_ROLE_RE.search(text, position)
        if match is None:
            break
        subject = _role_subject(text, match.start())
        if len(subject) < 2:
            position = match.start() + 1
            continue
        roles.append((match, subject))
        position = match.end()
    return roles


def _role_assignments(
    text: str,
) -> dict[tuple[str, str, frozenset[str]], set[str]]:
    conditions = _condition_terms(text) | frozenset(
        _SPACE.sub("", match.group(0)) for match in _THRESHOLD.finditer(text)
    )
    roles: dict[tuple[str, str, frozenset[str]], set[str]] = {}
    for match, subject in _explicit_roles(text):
        authority = match.group("authority").strip("的")
        if len(authority) < 2:
            continue
        binding = (subject, match.group("action"), conditions)
        roles.setdefault(binding, set()).add(authority)
    return roles


def _value_assertions(text: str) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for match in _VALUE_ASSERTION.finditer(text):
        head = _compact(match.group("head") + match.group("property"))
        value = _compact(match.group("value")).strip("的")
        if len(head) < 4 or not value:
            continue
        if value in {"我校", "本校"}:
            value = "华南农业大学"
        values.setdefault(head, set()).add(value)
    return values


def _numeric_branches(text: str) -> dict[str, set[str]]:
    branches: dict[str, set[str]] = {}
    for clause in _CLAUSE_SPLIT.split(text):
        facts = extract_facts(clause)
        if not facts:
            continue
        skeleton = clause
        for fact in sorted(facts, key=lambda item: len(item.raw), reverse=True):
            skeleton = skeleton.replace(fact.raw, "")
        skeleton = _compact(skeleton).strip("（）()【】[]")
        if len(skeleton) < 2:
            continue
        branches.setdefault(skeleton, set()).update(fact.canonical for fact in facts)
    return branches


@dataclass
class _Statement:
    claim: Claim
    text: str
    key: ClaimKey
    facts: frozenset[str]
    conditions: frozenset[str]
    roles: dict[tuple[str, str, frozenset[str]], set[str]]
    values: dict[str, set[str]]
    branches: dict[str, set[str]]
    modality: str
    negative: bool
    polarity_negative: bool
    canonical: str
    assertions: set[str]
    citations: list[Citation]


def _statement(claim: Claim, text: str, key: ClaimKey) -> _Statement:
    return _Statement(
        claim=claim,
        text=text,
        key=key,
        facts=_fact_set(text),
        conditions=_condition_terms(text),
        roles=_role_assignments(text),
        values=_value_assertions(text),
        branches=_numeric_branches(text),
        modality=_modality(text),
        negative=bool(_NEGATIVE.search(text)),
        polarity_negative=bool(_POLARITY_NEGATIVE.search(text)),
        canonical=_canonical(text),
        assertions=_assertion_grams(text),
        citations=list(claim.citations),
    )


def _authorities_compatible(left: set[str], right: set[str]) -> bool:
    # “学院”与“学院办公室”、“教务处”与“学校教务处”的隶属/简称关系未知，不能据此报冲突。
    return any(
        left_authority == right_authority
        or left_authority in right_authority
        or right_authority in left_authority
        for left_authority in left
        for right_authority in right
    )


def _role_conflict(left: _Statement, right: _Statement) -> bool:
    shared = set(left.roles) & set(right.roles)
    if not shared:
        return False
    return any(
        not _authorities_compatible(left.roles[binding], right.roles[binding])
        for binding in shared
    )


def _modality_conflict(left: _Statement, right: _Statement) -> bool:
    if left.negative or right.negative:
        return False
    if (
        _containment(left.assertions, right.assertions) < 0.85
        and _containment(right.assertions, left.assertions) < 0.85
    ):
        return False
    if left.modality and right.modality and left.modality != right.modality:
        return True
    return left.polarity_negative != right.polarity_negative


def _numeric_conflict(left: _Statement, right: _Statement) -> bool:
    for skeleton, left_values in left.branches.items():
        right_values = right.branches.get(skeleton)
        if not right_values or left_values == right_values:
            continue
        if left_values < right_values or right_values < left_values:
            continue
        return True
    return False


def _value_conflict(left: _Statement, right: _Statement) -> bool:
    for head, left_values in left.values.items():
        right_values = right.values.get(head)
        if not right_values:
            continue
        for left_value in left_values:
            for right_value in right_values:
                if left_value == right_value:
                    continue
                if left_value in right_value or right_value in left_value:
                    continue
                if _fact_set(left_value) and _fact_set(left_value) == _fact_set(right_value):
                    continue
                return True
    return False


def _conflict_reason(left: _Statement, right: _Statement) -> str:
    if left.key != right.key or left.conditions != right.conditions:
        return ""
    if _role_conflict(left, right):
        return "同一事项的经办主体/角色不一致"
    if _modality_conflict(left, right):
        return "同一事项的否定/义务方向相反"
    if _numeric_conflict(left, right):
        return "同一事项的数值不一致"
    if _value_conflict(left, right):
        return "同一事项的取值不一致"
    return ""


def _equivalent(left: _Statement, right: _Statement) -> bool:
    return (
        left.canonical == right.canonical
        and left.facts == right.facts
        and left.conditions == right.conditions
        and left.roles == right.roles
        and left.modality == right.modality
        and left.negative == right.negative
        and left.polarity_negative == right.polarity_negative
        and left.claim.kind == right.claim.kind
    )


def _merge_citations(primary: list[Citation], extra: list[Citation]) -> list[Citation]:
    merged = list(primary)
    seen = {
        (citation.article_id, citation.part_no, citation.quote_start, citation.quote_end)
        for citation in primary
    }
    for citation in extra:
        key = (citation.article_id, citation.part_no, citation.quote_start, citation.quote_end)
        if key not in seen:
            seen.add(key)
            merged.append(citation)
    return merged


def _shared_prefix(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        count += 1
    return count


def _same_documents(left: Claim, right: Claim) -> bool:
    def titles(claim: Claim) -> set[str]:
        return {citation.doc_title for citation in claim.citations if citation.doc_title}

    left_titles, right_titles = titles(left), titles(right)
    return bool(left_titles and right_titles and left_titles & right_titles)


def merge_claims(claims: list[Claim]) -> MergeResult:
    result = MergeResult()
    cleaned: list[tuple[Claim, str, ClaimKey]] = []
    for claim in claims:
        text = clean_claim_text(claim.text)
        if not text:
            result.notes.append(MergeNote("table_fragment", "省略一条表格残片结论"))
            result.dropped_claim_ids.append(claim.claim_id)
            continue
        cleaned.append((claim, text, _key(text)))

    # 1) order-independent retraction: a negative statement dies as soon as any
    #    positive claim covers the same item, whichever came first.
    positive_keys = {key for _, text, key in cleaned if not _NEGATIVE.search(text)}
    positive_prefixes = [_subject(text) for _, text, _ in cleaned if not _NEGATIVE.search(text)]
    survivors: list[tuple[Claim, str, ClaimKey]] = []
    for item in cleaned:
        claim, text, key = item
        if _NEGATIVE.search(text):
            subject = _subject(text)
            covered = key in positive_keys or any(
                _shared_prefix(prefix, subject) >= 6
                for prefix in positive_prefixes
                if len(prefix) >= 6 and len(subject) >= 6
            )
            if not covered:
                # “其余各系主任未列出” 由其他 claim 覆盖同一事项时同样撤销。
                topics = _topics(text)
                covered = any(
                    topic and topic in other_text and _same_documents(claim, other_claim)
                    for other_claim, other_text, _ in cleaned
                    if other_text != text and not _NEGATIVE.search(other_text)
                    for topic in topics
                )
            if covered:
                result.notes.append(
                    MergeNote("stale_missing", f"已撤销与明确规则并存的缺项表述：{text[:24]}")
                )
                result.dropped_claim_ids.append(claim.claim_id)
                continue
        survivors.append(item)

    # 2) group by key. Only a full canonical-equivalent statement may collapse;
    #    a shared key alone says nothing about whether two statements agree.
    groups: dict[ClaimKey, list[_Statement]] = {}
    for claim, text, key in survivors:
        groups.setdefault(key, []).append(_statement(claim, text, key))
    merged: list[_Statement] = []
    for entries in groups.values():
        entries.sort(key=lambda item: (item.text, item.claim.claim_id))
        distinct: list[_Statement] = []
        for entry in entries:
            twin = next((other for other in distinct if _equivalent(entry, other)), None)
            if twin is not None:
                twin.citations = _merge_citations(twin.citations, entry.citations)
                result.dropped_claim_ids.append(entry.claim.claim_id)
                if entry.claim.claim_id != twin.claim.claim_id:
                    result.replacement_claim_ids[entry.claim.claim_id] = twin.claim.claim_id
                result.notes.append(MergeNote("duplicate", f"合并重复结论：{entry.text[:24]}"))
                continue
            distinct.append(entry)
        if len(distinct) > 1:
            scopes = {_scope(item.text) for item in distinct if _scope(item.text)}
            if len(scopes) > 1:
                result.notes.append(
                    MergeNote(
                        "cross_version",
                        f"同年限不同版本并存，均保留：{'、'.join(sorted(scopes))}",
                    )
                )
            else:
                conflict: tuple[str, _Statement, _Statement] | None = None
                for index, left in enumerate(distinct):
                    for right in distinct[index + 1 :]:
                        reason = _conflict_reason(left, right)
                        if reason:
                            conflict = (reason, left, right)
                            break
                    if conflict is not None:
                        break
                if conflict is not None:
                    reason, left, right = conflict
                    first, second = sorted((left.text, right.text))
                    result.notes.append(
                        MergeNote(
                            "conflict",
                            f"{reason}，均保留未择一，请对照原文核实：{first[:24]}；{second[:24]}",
                        )
                    )
        merged.extend(distinct)

    result.claims = [
        item.claim.model_copy(update={"text": item.text, "citations": item.citations})
        for item in merged
    ]
    result.dropped_claim_ids = sorted(
        set(result.dropped_claim_ids) - {c.claim_id for c in result.claims}
    )
    return result


__all__ = [
    "ClaimKey",
    "MergeNote",
    "MergeResult",
    "clean_claim_text",
    "merge_claims",
]

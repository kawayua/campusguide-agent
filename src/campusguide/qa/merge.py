"""Merge verified claims before rendering (R6 / 报告 A1、A6).

Rules the report demands:

* the claim key carries object, attribute, level, class type, cohort/version and
  applicability condition, so two correct facts for different cohorts are never
  collapsed;
* near-duplicates only collapse when numbers, negation, time and scope agree;
* a real conflict is never resolved by citation count or input order: both
  statements are listed with a "verify against the latest notice" note;
* a "未收录/未提及" statement is retracted whenever another claim covers the same
  item, regardless of order;
* notes are typed (duplicate / conflict / stale_missing) so only substantive
  gaps can affect completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from campusguide.domain.models import Claim
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
_MODAL = re.compile(r"必须|不得|禁止|可以|可为|应当|应|须|一律|均")

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
    cleaned = re.sub(r"^[\s、，,；;：:]+|[\s、，,；;：:]+$", "", cleaned)
    return cleaned


def _compact(text: str) -> str:
    return _SPACE.sub("", text)


def _trigrams(text: str) -> set[str]:
    compact = _compact(text)
    return {compact[index : index + 3] for index in range(max(0, len(compact) - 2))}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


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
    head = _RANGE.sub("", head)
    head = _COHORT.sub("", head)
    head = _LEVEL.sub("", head)
    head = _CLASS.sub("", head)
    head = _NUMBER_TOKEN.sub("", head)
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


def _fact_values(text: str) -> tuple[tuple[str, ...], bool, str]:
    numbers = tuple(sorted(fact.canonical for fact in extract_facts(text)))
    modal = _MODAL.search(text)
    return numbers, bool(_NEGATIVE.search(text)), modal.group(0) if modal else ""


def _same_facts(left: str, right: str) -> bool:
    return _fact_values(left) == _fact_values(right)


def _shared_prefix(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(left, right):
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
    cleaned: list[tuple[Claim, str, ClaimKey, set[str]]] = []
    for claim in claims:
        text = clean_claim_text(claim.text)
        if not text:
            result.notes.append(MergeNote("table_fragment", "省略一条表格残片结论"))
            result.dropped_claim_ids.append(claim.claim_id)
            continue
        cleaned.append((claim, text, _key(text), _trigrams(text)))

    # 1) order-independent retraction: a negative statement dies as soon as any
    #    positive claim covers the same item, whichever came first.
    positive_keys = {
        key for _, text, key, _ in cleaned if not _NEGATIVE.search(text)
    }
    positive_prefixes = [
        _subject(text) for _, text, _, _ in cleaned if not _NEGATIVE.search(text)
    ]
    survivors: list[tuple[Claim, str, ClaimKey, set[str]]] = []
    for item in cleaned:
        claim, text, key, _ = item
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
                    topic
                    and topic in other_text
                    and _same_documents(claim, other_claim)
                    for other_claim, other_text, _, _ in cleaned
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

    # 2) group by key; duplicates collapse only when every fact agrees.
    groups: dict[ClaimKey, list[tuple[Claim, str, set[str]]]] = {}
    for claim, text, key, trigrams in survivors:
        groups.setdefault(key, []).append((claim, text, trigrams))
    merged: list[tuple[Claim, str]] = []
    for key, entries in groups.items():
        entries.sort(key=lambda entry: entry[1])
        duplicate_of: dict[str, str] = {}
        distinct: list[tuple[Claim, str]] = []
        for claim, text, trigrams in entries:
            twin = next(
                (
                    other
                    for other in distinct
                    if _jaccard(trigrams, _trigrams(other[1])) >= 0.92
                    and _same_facts(other[1], text)
                ),
                None,
            )
            if twin is not None:
                duplicate_of[claim.claim_id] = twin[0].claim_id
                result.dropped_claim_ids.append(claim.claim_id)
                result.notes.append(MergeNote("duplicate", f"合并重复结论：{text[:24]}"))
                continue
            distinct.append((claim, text))
        if len(distinct) > 1:
            scopes = {_scope(text) for _, text in distinct if _scope(text)}
            if len(scopes) > 1:
                result.notes.append(
                    MergeNote(
                        "cross_version",
                        f"同年限不同版本并存，均保留：{'、'.join(sorted(scopes))}",
                    )
                )
            else:
                result.notes.append(
                    MergeNote(
                        "conflict",
                        "同一事项存在两种表述，已并列列出，请以最新正式通知为准核实："
                        + distinct[0][1][:24],
                    )
                )
        merged.extend((claim, text) for claim, text in distinct)

    result.claims = [claim.model_copy(update={"text": text}) for claim, text in merged]
    result.dropped_claim_ids = sorted(set(result.dropped_claim_ids) - {c.claim_id for c in result.claims})
    return result


__all__ = [
    "ClaimKey",
    "MergeNote",
    "MergeResult",
    "clean_claim_text",
    "merge_claims",
]
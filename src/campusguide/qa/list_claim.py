"""Deterministic list claim for "which X are there" questions (098 / 方案 A4).

When the answer enumerates the wrong type (labs filed as demonstration centres) and
the rewrite drops the claim instead of listing the asked type, the asked items are
still sitting verbatim in the source. This module composes a minimal claim that only
repeats those names, so the answer can list them instead of silently omitting them.

Rules: every name is a verbatim substring of one evidence article; at most eight
items; no numbers, dates or evaluative words are added; the claim still goes through
the normal validator and verifier.
"""

from __future__ import annotations

import re

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.models import Evidence
from campusguide.verification.conditions import check_conditions
from campusguide.verification.entities import items_of_type, type_span

MAX_ITEMS = 8

_TYPE_LABEL = {
    "实验教学示范中心": "实验教学示范中心",
    "国家级实验教学示范中心": "国家级实验教学示范中心",
    "省级实验教学示范中心": "省级实验教学示范中心",
    "示范中心": "实验教学示范中心",
    "重点实验室": "重点实验室",
    "实验室": "实验室",
    "研究中心": "研究中心",
    "工程中心": "工程中心",
}

# The interrogative must bind to the entity type, not merely co-occur with it:
# “实验室安全有哪些规定” asks about regulations, not laboratories.
_LIST_MARKER = r"(?:有哪些|哪几个|哪几家|哪几所|哪些)"
_LIST_MODIFIER = (
    r"(?:国家级|省级|省部级|市级|校级|院级|重点|主要|所有|全部|现有|在建|新增|"
    r"直属|下属|内设|各类|各|个|所|家|的)*"
)
_LIST_SUFFIX = r"(?:的?(?:名称|名单|清单|名录))?"
_LIST_TAIL = r"(?:等)?(?:[？?。！!，,；;]|$|呢|呀)"


def _asks_for_list(question: str, type_word: str) -> bool:
    """True only when an enumeration interrogative targets ``type_word`` itself."""
    text = re.sub(r"\s+", "", question)
    word = re.escape(type_word)
    head = _LIST_MARKER + _LIST_MODIFIER + word + _LIST_SUFFIX + _LIST_TAIL
    tail = word + _LIST_SUFFIX + r"(?:分别)?(?:是)?" + _LIST_MARKER + _LIST_SUFFIX + _LIST_TAIL
    return re.search(head, text) is not None or re.search(tail, text) is not None


def _subject(evidence: Evidence) -> str:
    title = (evidence.article.doc_title or "").strip()
    title = re.sub(r"(?:人才培养方案|教学实验平台|管理办法|实施细则).*$", "", title).strip()
    return title or ""


def asked_entity_type(question: str) -> str:
    """The single entity type an enumeration question asks about (most specific first).

    A bare mention is not enough: “实验室安全有哪些规定” mentions 实验室 but asks
    about regulations. Every caller wants list intent, so the gate lives here.
    """
    for word in (
        "国家级实验教学示范中心",
        "省级实验教学示范中心",
        "实验教学示范中心",
        "示范中心",
        "重点实验室",
        "研究中心",
        "工程中心",
        "实验室",
    ):
        if word in question and _asks_for_list(question, word):
            return word
    return ""


def build_list_claim(question: str, evidence: list[Evidence]) -> ClaimDraft | None:
    """Compose a claim that lists the asked entity type straight from the source."""
    type_word = asked_entity_type(question)
    if not type_word or not _asks_for_list(question, type_word):
        return None
    best: tuple[Evidence, list[str], tuple[int, int]] | None = None
    for item in evidence:
        text = item.article.text or ""
        names = items_of_type(text, type_word)
        if not names or len(names) > MAX_ITEMS:
            continue
        span = type_span(text, type_word)
        if span is None:
            continue
        if best is None or len(names) > len(best[1]):
            best = (item, names, span)
    if best is None:
        return None
    item, names, (start, end) = best
    text = item.article.text or ""
    quote = text[start:end]
    # The quote must literally contain every name; otherwise the claim would
    # paraphrase the source.
    if any(name not in quote for name in names):
        return None
    label = _TYPE_LABEL.get(type_word, type_word)
    subject = _subject(item)
    body = "、".join(names)
    claim_text = f"{subject}的{label}包括{body}。" if subject else f"{label}包括{body}。"
    # 名称之外不得引入数字/时间等可核对事实。
    if re.search(r"\d", claim_text):
        return None
    return ClaimDraft(
        claim_id="list_fallback_1",
        text=claim_text,
        kind="fact",
        citations=[CitationDraft(article_id=item.article.article_id, quote=quote)],
    )


def claim_lists_type(claims: list, type_word: str) -> bool:
    """True when at least one claim already enumerates two or more items of the type."""
    for claim in claims:
        text = getattr(claim, "text", "") or ""
        names = items_of_type(text, type_word)
        if len(names) >= 2:
            return True
    return False


def claim_lists_type_cleanly(claims: list, type_word: str) -> bool:
    """同 ``claim_lists_type``，但排除“顺带混入其它类型”的清单。

    098：旧结论把实验室一起算成示范中心，虽然点名了三个中心，也不是合格的
    类型清单，必须继续触发逐字引用兜底。
    """
    for claim in claims:
        if not claim_lists_type([claim], type_word):
            continue
        quotes = [
            citation.quote
            for citation in getattr(claim, "citations", []) or []
            if getattr(citation, "quote", "")
        ]
        if any(finding.kind == "category" for finding in check_conditions(claim.text, quotes)):
            continue
        return True
    return False


__all__ = [
    "asked_entity_type",
    "build_list_claim",
    "claim_lists_type",
    "claim_lists_type_cleanly",
]
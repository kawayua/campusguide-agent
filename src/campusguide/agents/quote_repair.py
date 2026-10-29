"""W6-A2：citation quote 连续性与范围/数字覆盖的生成侧修复。

这里只做“检测 + 一次重写重试”，不改变 W3 的确定性核对：
- 不修改 Verifier / ClaimValidator；
- 修复失败时保留原 draft，交给 Verifier 丢弃；
- 不把模型重写当成新的核对依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from campusguide.domain.models import Evidence
from campusguide.verification.facts import check_evidence_fact_coverage
from campusguide.verification.quotes import locate_quote

MIN_QUOTE_CHARS = 6


@dataclass(frozen=True)
class QuoteProblem:
    index: int
    claim_id: str
    reasons: tuple[str, ...]


@dataclass
class QuoteRepairResult:
    drafts: list[Any]
    changed: bool = False
    remaining: list[str] = field(default_factory=list)


def _claim_label(draft: Any, index: int) -> str:
    return draft.claim_id or f"c{index + 1}"


def _citation_problems(draft: Any, by_article: dict[str, list[Evidence]]) -> list[str]:
    reasons: list[str] = []
    for citation in draft.citations:
        quote = (citation.quote or "").strip()
        article_id = citation.article_id or ""
        if not quote:
            reasons.append(f"{article_id or '空 article_id'}：quote 为空")
            continue
        if "…" in quote or "..." in quote:
            reasons.append(f"{article_id}：quote 含省略号")
        if len("".join(quote.split())) < MIN_QUOTE_CHARS:
            reasons.append(f"{article_id}：quote 不足 {MIN_QUOTE_CHARS} 字")
        candidates = by_article.get(article_id, [])
        if not candidates:
            reasons.append(f"{article_id}：article_id 不在本轮证据")
            continue
        if not any(locate_quote(item.article.text, quote) for item in candidates):
            reasons.append(f"{article_id}：quote 不是证据正文的连续子串")
    return reasons


def find_quote_problems(drafts: list[Any], evidence: list[Evidence]) -> list[QuoteProblem]:
    by_article: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_article.setdefault(item.article.article_id, []).append(item)

    problems: list[QuoteProblem] = []
    for index, draft in enumerate(drafts):
        reasons = _citation_problems(draft, by_article)
        if draft.text and draft.citations:
            locatable_quotes = [
                citation.quote
                for citation in draft.citations
                if citation.quote
                and any(
                    locate_quote(item.article.text, citation.quote)
                    for item in by_article.get(citation.article_id, [])
                )
            ]
            if locatable_quotes:
                coverage = check_evidence_fact_coverage(
                    draft.text,
                    locatable_quotes,
                    [
                        item.article
                        for citation in draft.citations
                        for item in by_article.get(citation.article_id, [])
                        if locate_quote(item.article.text, citation.quote)
                    ],
                )
                if coverage.uncovered:
                    facts = "、".join(fact.canonical for fact in coverage.uncovered)
                    reasons.append(f"claim 的数字/期限未被 quote 覆盖：{facts}")
        if reasons:
            problems.append(
                QuoteProblem(
                    index=index,
                    claim_id=_claim_label(draft, index),
                    reasons=tuple(reasons),
                )
            )
    return problems


def build_repair_messages(
    base_messages: list[dict[str, str]],
    assistant_content: str,
    drafts: list[Any],
    problems: list[QuoteProblem],
) -> list[dict[str, str]]:
    lines = [
        "上面的 claims JSON 中有 citation 不符合输出契约。请只重写下列 claim 的 citations，"
        "不要解释，不要输出 Markdown 代码块。",
        "硬性要求：",
        "1. 每个 citation.quote 必须是对应 article_id 证据正文中的一个连续子串；",
        "2. 若结论在引导句、分项在下方，必须从引导句连续复制到该分项，允许包含中间分项；"
        "不得用省略号，不得跳过中间内容，不得把不相邻的句子拼接；",
        "3. 数字、金额、期限、角色必须由正文支持；仅适用主体和入学届别可由可信元数据支持；",
        "4. 无法用连续 quote 支持时，删除该 claim，不要保留无法定位的 quote；",
        '5. 输出完整 claims JSON（未失败的 claim 原样保留），结构仍为 {"claims": [...]}。',
        "",
        "失败项：",
    ]
    for problem in problems:
        lines.append(f"- {problem.claim_id}：{'；'.join(problem.reasons)}")
    lines.append("")
    lines.append("原 claims JSON：")
    lines.append(assistant_content)
    return [*base_messages, {"role": "user", "content": "\n".join(lines)}]


def merge_repairs(
    original: list[Any],
    repaired: list[Any],
    problems: list[QuoteProblem],
) -> QuoteRepairResult:
    failing = {problem.index: problem for problem in problems}
    if not repaired:
        # 模型明确返回空 claims：按“无法修复则丢弃”处理失败项，保留其他 claim。
        if not failing:
            return QuoteRepairResult(drafts=list(original), changed=False)
        merged = [draft for index, draft in enumerate(original) if index not in failing]
        return QuoteRepairResult(drafts=merged, changed=True)
    by_id = {item.claim_id: item for item in repaired if item.claim_id}
    same_length = len(repaired) == len(original)

    merged: list[Any] = []
    changed = False
    for index, draft in enumerate(original):
        problem = failing.get(index)
        if problem is None:
            merged.append(draft)
            continue
        replacement = by_id.get(draft.claim_id) if draft.claim_id else None
        if replacement is None and same_length:
            replacement = repaired[index]
        if replacement is None:
            # 修复输出删除了该 claim：按契约丢弃。
            changed = True
            continue
        merged.append(replacement)
        changed = True
    return QuoteRepairResult(drafts=merged, changed=changed)


def remaining_problem_ids(drafts: list[Any], evidence: list[Evidence]) -> list[str]:
    return [problem.claim_id for problem in find_quote_problems(drafts, evidence)]

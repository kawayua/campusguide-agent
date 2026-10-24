"""把校验通过的 claims 渲染成答案 Markdown。

W2/W3 的正文完全由通过核对的 claims 拼接，不额外生成事实句。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from campusguide.domain.enums import BlockType
from campusguide.domain.models import Citation, Claim

DISCLAIMER = "演示系统不构成官方答复，具体以学校官网为准。"
LOW_DATE_NOTICE = "日期为文号年份/月份锚点，非精确发布日。"
PARTIAL_NOTICE = "部分结论未通过引用核对，已省略；以下仅为可核实内容。"


@dataclass
class FinalizeResult:
    answer_markdown: str
    citations: list[Citation] = field(default_factory=list)


def citation_label(citation: Citation) -> str:
    if citation.block_type is BlockType.SECTION:
        return f"{citation.doc_title} {citation.article_no}（章节/段落）"
    if citation.block_type is BlockType.ARTICLE:
        return f"{citation.doc_title} {citation.article_no}"
    return f"{citation.doc_title} {citation.article_no}（{citation.block_type.value}）"


def _quote_block(quote: str) -> str:
    lines = quote.strip().splitlines() or [quote.strip()]
    return "\n".join(f"> {line}" for line in lines)


def finalize(
    claims: list[Claim],
    *,
    partial: bool = False,
    dropped_count: int = 0,
) -> FinalizeResult:
    if not claims:
        raise ValueError("finalize 不接受空 claims")

    lines: list[str] = []
    if partial:
        lines.extend([f"> {PARTIAL_NOTICE}", f"> 已省略 {dropped_count} 条结论。", ""])
    lines.extend(["**结论要点：**", ""])
    cards: list[Citation] = []
    seen_cards: set[tuple[str, int, int, int]] = set()
    for index, claim in enumerate(claims, start=1):
        labels = "".join(f" [{citation_label(citation)}]" for citation in claim.citations)
        lines.append(f"{index}. {claim.text}{labels}")
        for citation in claim.citations:
            key = (
                citation.article_id,
                citation.part_no,
                citation.quote_start,
                citation.quote_end,
            )
            if key not in seen_cards:
                seen_cards.add(key)
                cards.append(citation)

    lines.extend(["", "**依据：**", ""])
    for citation in cards:
        label = citation_label(citation)
        lines.append(f"- [{label}]({citation.source_url})")
        lines.extend(_quote_block(citation.quote).splitlines())
        if citation.date_confidence == "low":
            lines.append(f"> {LOW_DATE_NOTICE}")
        lines.append("")

    lines.extend(["---", DISCLAIMER])
    return FinalizeResult(answer_markdown="\n".join(lines).strip(), citations=cards)

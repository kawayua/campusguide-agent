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
    semantic_unchecked: bool = False,
) -> FinalizeResult:
    if not claims:
        raise ValueError("finalize 不接受空 claims")

    lines: list[str] = []
    if semantic_unchecked:
        lines.extend([f"> {SEMANTIC_UNCHECKED_NOTICE}", ""])
    if partial:
        if dropped_count:
            lines.extend([f"> {PARTIAL_NOTICE}", f"> 已省略 {dropped_count} 条结论。", ""])
        else:
            lines.extend(["> 以下为已核实内容；尚缺的回答部分见后文说明。", ""])
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


SEMANTIC_UNCHECKED_NOTICE = "本次未完成语义核对，仅硬校验通过。"
FLOW_POLICY_MISSING_NOTICE = "本问未检索到可引用的政策条款，未生成政策结论。"


def _policy_block(
    claims: list[Claim],
    *,
    partial: bool,
    dropped_count: int,
) -> FinalizeResult:
    return finalize(claims, partial=partial, dropped_count=dropped_count)


def finalize_flow(
    flow,
    checklist,
    *,
    status: str,
    handoff,
    closed: bool = False,
    extra_lines: list[str] | None = None,
) -> str:
    """流程回答只渲染确定性 checklist；不掺入 Advisor 的自由文本。"""
    from campusguide.flows.engine import FlowEvaluation, render_checklist_text

    evaluation = FlowEvaluation(flow=flow, status=status, checklist=list(checklist))
    lines = [render_checklist_text(evaluation)]
    if closed:
        lines.append("下一批次的时间、名额和材料以学校/学院当年通知为准；本流程不推断下一年日期。")
    for line in extra_lines or []:
        lines.append(line)
    if handoff is not None and handoff.department:
        lines.append(f"人工入口：{handoff.department}")
        if handoff.url:
            lines.append(str(handoff.url))
        if handoff.note:
            lines.append(handoff.note)
    lines.extend(["---", DISCLAIMER])
    return "\n".join(lines).strip()


def finalize_mixed(
    claims: list[Claim],
    *,
    flow,
    checklist,
    status: str,
    handoff,
    closed: bool = False,
    partial: bool = False,
    dropped_count: int = 0,
    semantic_unchecked: bool = False,
    extra_lines: list[str] | None = None,
) -> FinalizeResult:
    """办理路径 + 政策说明分区合并；冲突时 checklist 优先。"""
    flow_text = finalize_flow(
        flow,
        checklist,
        status=status,
        handoff=handoff,
        closed=closed,
        extra_lines=extra_lines,
    )
    # 去掉流程块自己的免责声明，由混合回答统一追加一次。
    marker = "\n---\n" + DISCLAIMER
    if flow_text.endswith(marker):
        flow_text = flow_text[: -len(marker)].rstrip()
    policy = _policy_block(claims, partial=partial, dropped_count=dropped_count)
    policy_text = policy.answer_markdown
    if policy_text.endswith(marker):
        policy_text = policy_text[: -len(marker)].rstrip()
    lines = [
        "**办理路径（流程事实以 checklist 为准）：**",
        "",
        flow_text,
        "",
        "**政策说明：**",
        "",
        policy_text,
        "",
        "---",
        DISCLAIMER,
    ]
    if semantic_unchecked:
        lines.insert(0, f"> {SEMANTIC_UNCHECKED_NOTICE}")
        lines.insert(1, "")
    return FinalizeResult(answer_markdown="\n".join(lines).strip(), citations=policy.citations)


def finalize_abstain(
    *,
    message: str,
    handoff,
    missing_slots: list[str] | None = None,
    warnings: list[str] | None = None,
    semantic_unchecked: bool = False,
) -> str:
    lines: list[str] = []
    if semantic_unchecked:
        lines.extend([f"> {SEMANTIC_UNCHECKED_NOTICE}", ""])
    lines.append(f"**暂时无法作答：** {message}")
    if missing_slots:
        lines.append("")
        lines.append("**缺少的信息：** " + "、".join(missing_slots))
    if warnings:
        lines.append("")
        lines.extend(f"- {item}" for item in warnings)
    lines.extend(["", "**下一步：**"])
    if handoff is not None and handoff.department:
        lines.append(f"- 可联系：{handoff.department}")
        if handoff.url:
            lines.append(f"- 官方入口：{handoff.url}")
        if handoff.note:
            lines.append(f"- {handoff.note}")
    else:
        lines.append("- 请补充具体事项，或联系学校相关负责部门。")
    lines.extend(["", "---", DISCLAIMER])
    return "\n".join(lines).strip()


def finalize_flow_without_claims(
    *,
    flow,
    checklist,
    status: str,
    handoff,
    closed: bool = False,
    reason_message: str = FLOW_POLICY_MISSING_NOTICE,
) -> str:
    text = finalize_flow(
        flow,
        checklist,
        status=status,
        handoff=handoff,
        closed=closed,
        extra_lines=[f"> {reason_message}"],
    )
    return text

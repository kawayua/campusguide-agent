"""W8 UI 视图模型：把 ``ConversationReply`` 转成纯 HTML / 标签。

这一层不依赖 Gradio，也不读 YAML、Qdrant 或 MCP；只消费 UI 数据契约，
方便用 fake reply 做离线测试。所有正文与外部文本都做 HTML 转义。
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from campusguide.observability.redact import redact_sensitive

OUTCOME_META: dict[str, dict[str, str]] = {
    "ANSWERED": {"label": "已核实回答", "tone": "success", "title": "已核实回答"},
    "PARTIAL_ANSWER": {
        "label": "部分回答",
        "tone": "warn",
        "title": "部分回答，未确认项已标注",
    },
    "CLARIFY": {"label": "需要补充信息", "tone": "info", "title": "需要补充信息"},
    "ABSTAIN": {"label": "未作答", "tone": "abstain", "title": "未作答"},
    "NO_EVIDENCE": {
        "label": "未作答 · 无可引用依据",
        "tone": "abstain",
        "title": "未检索到可引用依据",
    },
    "UNVERIFIABLE": {
        "label": "未作答 · 未通过核对",
        "tone": "abstain",
        "title": "未通过引用核对",
    },
    "ONLY_REPEALED": {
        "label": "未作答 · 仅废止版本",
        "tone": "abstain",
        "title": "只检索到已废止版本",
    },
    "VERSION_MISMATCH": {
        "label": "未作答 · 版本不符",
        "tone": "abstain",
        "title": "材料不在当前有效期内",
    },
    "VERSION_CONFLICT": {
        "label": "未作答 · 版本冲突",
        "tone": "abstain",
        "title": "多个版本并存，无法确定",
    },
    "HISTORICAL_NOT_SUPPORTED": {
        "label": "历史版本暂不支持",
        "tone": "neutral",
        "title": "只回答现行有效制度",
    },
    "PRIVACY_REQUEST": {
        "label": "未作答 · 涉及个人信息",
        "tone": "abstain",
        "title": "涉及个人信息，不查询",
    },
    "OUT_OF_SCOPE": {
        "label": "超出范围",
        "tone": "neutral",
        "title": "超出回答范围",
    },
    "INVALID_REQUEST": {"label": "请输入具体问题", "tone": "neutral", "title": "请输入具体问题"},
    "GENERATION_FAILED": {
        "label": "生成服务暂不可用",
        "tone": "abstain",
        "title": "生成服务暂不可用",
    },
    "NO_VALID_CLAIM": {
        "label": "未生成可核对结论",
        "tone": "abstain",
        "title": "未生成可核对结论",
    },
    "INDEX_UNAVAILABLE": {
        "label": "索引不可用",
        "tone": "abstain",
        "title": "索引不可用或与 manifest 不一致",
    },
    "TOOL_UNAVAILABLE": {
        "label": "检索工具不可用",
        "tone": "abstain",
        "title": "检索工具不可用",
    },
}

CHECKLIST_STATUS: dict[str, dict[str, str]] = {
    "available": {"label": "可办理", "tone": "available"},
    "conditional": {"label": "待确认", "tone": "conditional"},
    "blocked": {"label": "受阻", "tone": "blocked"},
    "closed": {"label": "已截止", "tone": "closed"},
    "done": {"label": "已完成", "tone": "done"},
    "not_applicable": {"label": "不适用", "tone": "na"},
}

SLOT_LABELS: dict[str, str] = {
    "level": "身份（本科生 / 研究生）",
    "enrollment_status": "是否在校",
    "purpose": "证明用途",
    "delivery": "领取方式",
    "college": "学院",
    "award_type": "奖项类型",
    "grade": "年级",
    "has_record": "纪律处分情况",
    "has_financial_aid": "家庭经济困难认定",
    "is_graduating": "是否应届本科毕业生",
    "has_recommendation_quota": "学院推免名额",
    "in_recommendation_list": "是否在推免名单",
}

VALUE_LABELS: dict[str, str] = {
    "undergraduate": "本科生",
    "graduate": "研究生",
    "enrolled": "在校生",
    "graduated": "已毕业",
    "study": "升学",
    "employment": "就业",
    "internship": "实习",
    "overseas": "出国留学",
    "other": "其他",
    "self-print": "自助打印",
    "window": "窗口领取",
    "entrust": "委托他人",
    "info": "数信学院",
    "agri": "农学院",
    "merit": "国家奖学金",
    "inspirational": "国家励志奖学金",
    "grant": "国家助学金",
    "true": "是",
    "false": "否",
    "yes": "是",
    "no": "否",
}

# 只保留展示所需的字段；不把内部原文引用或 MCP payload 带进 UI 状态。
_REPLY_FIELDS = (
    "text",
    "flow_id",
    "status",
    "checklist",
    "next_slot",
    "missing_slots",
    "ambiguous_flow_ids",
    "warnings",
    "closed",
    "claims",
    "citations",
    "outcome",
    "decision",
    "handoff",
    "trace",
    "clarification",
)


def _plain(value: Any) -> Any:
    """把 Pydantic / dataclass / date / URL 深度转成 JSON 友好值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(mode="json"))
    return str(value)


def field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def reply_to_dict(reply: Any) -> dict[str, Any]:
    return {name: _plain(field(reply, name)) for name in _REPLY_FIELDS}


def outcome_meta(code: str | None) -> dict[str, str]:
    return OUTCOME_META.get(code or "", OUTCOME_META["ABSTAIN"])


def outcome_badge_html(code: str | None, *, closed: bool = False) -> str:
    meta = outcome_meta(code)
    badge = (
        f'<span class="w8-badge w8-badge-{meta["tone"]}">'
        f'<span class="w8-dot"></span>{html.escape(meta["label"])}</span>'
    )
    if closed:
        badge += (
            '<span class="w8-badge w8-badge-closed"><span class="w8-dot"></span>批次已截止</span>'
        )
    return badge


def short_text(value: str, limit: int = 18) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slot_label(name: str) -> str:
    return SLOT_LABELS.get(name, name)


def value_label(value: str) -> str:
    return VALUE_LABELS.get(str(value), str(value))


def format_deadline(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw).replace("T", " ").replace("+08:00", "")
    if " " in text:
        date_part, time_part = text.split(" ", 1)
        chunks = time_part.split(":")
        if len(chunks) >= 3:
            time_part = ":".join(chunks[:2])
        text = f"{date_part} {time_part}"
    return text


def format_duration(ms: Any) -> str:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return "—"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value}ms"


def reply_duration_ms(reply: Any) -> int:
    trace = field(reply, "trace") or []
    total = 0
    for item in trace:
        try:
            total += int(field(item, "duration_ms", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def assistant_message_html(reply: Any) -> str:
    """Chatbot 的助手消息：状态徽标 + Finalize 文本（Markdown）。

    只用状态徽标的 HTML；正文仍交给 Chatbot 的 Markdown 渲染器，避免把
    用户原文拼进 HTML。状态徽标用 ``span`` 而非块级 div，保证后续 Markdown
    仍能被解析。
    """
    code = str(field(reply, "outcome", "") or "")
    closed = bool(field(reply, "closed", False))
    text = str(field(reply, "text", "") or "").strip()
    badges = outcome_badge_html(code, closed=closed)
    return f'<span class="w8-turn-badges">{badges}</span>\n\n{text}'


def _status_badge(status: str) -> str:
    meta = CHECKLIST_STATUS.get(status, CHECKLIST_STATUS["conditional"])
    return f'<span class="w8-step-badge w8-step-{meta["tone"]}">{html.escape(meta["label"])}</span>'


def visible_flow_step_count(reply: Any) -> int:
    items = list(field(reply, "checklist", []) or [])
    return sum(1 for item in items if str(field(item, "status", "")) != "not_applicable")


def _unique_flow_citations(checklist: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for item in checklist:
        if str(field(item, "status", "")) == "not_applicable":
            continue
        for article_id in field(item, "citations", []) or []:
            value = str(article_id)
            if value not in seen:
                seen.append(value)
    return seen


def _flow_citation_parts(article_id: str) -> tuple[str, str]:
    doc_part, _, article_no = article_id.partition("#")
    doc_id, _, version = doc_part.partition("@")
    return doc_id, article_no or version or doc_id


def _flow_citation_label(article_id: str) -> str:
    doc_id, article_no = _flow_citation_parts(article_id)
    return f"{doc_id} · {article_no}"


def _checklist_counts(items: list[Any]) -> dict[str, int]:
    counts = {key: 0 for key in CHECKLIST_STATUS}
    for item in items:
        status = str(field(item, "status", "conditional"))
        if status in counts:
            counts[status] += 1
    return counts


def _empty_state(title: str, text: str, extra: str = "") -> str:
    return (
        '<div class="w8-empty">'
        f'<div class="w8-empty-icon">·</div><h3>{html.escape(title)}</h3>'
        f"<p>{html.escape(text)}</p>{extra}</div>"
    )


def checklist_html(
    reply: Any,
    *,
    show_not_applicable: bool = False,
    status_filter: str = "all",
) -> str:
    if reply is None:
        return _empty_state("还没有结构化结果", "提一个问题后，最后一轮的办理清单会显示在这里。")
    items = list(field(reply, "checklist", []) or [])
    if not items:
        outcome = str(field(reply, "outcome", "") or "")
        missing = list(field(reply, "missing_slots", []) or [])
        extra = ""
        if missing:
            chips = "".join(f"<span>{html.escape(slot_label(slot))}</span>" for slot in missing)
            extra = f'<div class="w8-missing-list">{chips}</div>'
        title = "等待补充信息" if outcome == "CLARIFY" else "本轮没有办理清单"
        text = (
            "回答上方问题后生成办理清单。"
            if outcome == "CLARIFY"
            else "拒答或纯政策轮不生成清单；引用与 handoff 见对话或引用 tab。"
        )
        return _empty_state(title, text, extra)

    counts = _checklist_counts(items)
    visible = [item for item in items if str(field(item, "status", "")) != "not_applicable"]
    visible_total = len(visible)
    displayed_total = len(items) if show_not_applicable else visible_total

    filtered = list(items)
    if not show_not_applicable:
        filtered = [item for item in filtered if str(field(item, "status", "")) != "not_applicable"]
    if status_filter != "all":
        filtered = [item for item in filtered if str(field(item, "status", "")) == status_filter]

    bar_parts = []
    for status in ("closed", "blocked", "conditional", "available", "done"):
        count = counts.get(status, 0)
        if count:
            width = count / max(visible_total, 1) * 100
            bar_parts.append(f'<i class="w8-sum-{status}" style="width:{width:.1f}%"></i>')
    legend = []
    for status in ("closed", "blocked", "conditional", "available", "done"):
        count = counts.get(status, 0)
        if count:
            meta = CHECKLIST_STATUS[status]
            legend.append(
                f'<span><i class="w8-dot-{meta["tone"]}"></i>{meta["label"]} {count}</span>'
            )
    if counts.get("not_applicable"):
        suffix = "已显示" if show_not_applicable else "默认隐藏"
        legend.append(
            f'<span><i class="w8-dot-na"></i>不适用 {counts["not_applicable"]}（{suffix}）</span>'
        )

    summary = (
        '<div class="w8-cl-summary">'
        '<div class="w8-cl-sum-top">'
        f"<strong>{displayed_total} 个步骤</strong>"
        f"<span>{counts.get('closed', 0)} 已截止 · {counts.get('conditional', 0)} 待确认</span>"
        "</div>"
        f'<div class="w8-sum-bar">{"".join(bar_parts)}</div>'
        f'<div class="w8-sum-legend">{"".join(legend)}</div>'
        "</div>"
    )

    cards: list[str] = []
    for index, item in enumerate(filtered, start=1):
        status = str(field(item, "status", "conditional"))
        closed = status == "closed"
        deadline = field(item, "deadline")
        deadline_note = field(item, "deadline_note")
        status_note = field(item, "status_note")
        blocked_reason = field(item, "blocked_reason")
        duplicate_closed = bool(
            closed and deadline and status_note and str(status_note).startswith("本批次截止")
        )
        meta_lines = []
        if deadline:
            meta_lines.append(
                '<div class="w8-meta-line w8-meta-closed">'
                f"<span>◷</span><span><strong>{'本批次截止' if closed else '截止'}：</strong>"
                f"{html.escape(format_deadline(deadline))}</span></div>"
            )
        elif deadline_note:
            meta_lines.append(
                '<div class="w8-meta-line">'
                f"<span>◷</span><span><strong>时限：</strong>"
                f"{html.escape(str(deadline_note))}</span></div>"
            )
        department = field(item, "department")
        if department:
            meta_lines.append(
                '<div class="w8-meta-line">'
                f"<span>⌂</span><span>{html.escape(str(department))}</span></div>"
            )
        materials = field(item, "materials", []) or []
        material_html = ""
        if materials:
            material_html = (
                '<div class="w8-materials">'
                + "".join(
                    f'<span class="w8-material">{html.escape(str(m))}</span>' for m in materials
                )
                + "</div>"
            )
        note_html = ""
        if blocked_reason:
            note_html = (
                '<div class="w8-status-note w8-status-blocked">'
                f"受阻原因：{html.escape(str(blocked_reason))}</div>"
            )
        elif status_note and not duplicate_closed:
            tone = "w8-status-closed" if closed else ""
            note_html = f'<div class="w8-status-note {tone}">{html.escape(str(status_note))}</div>'
        citations = list(field(item, "citations", []) or [])
        citation_html = ""
        if citations:
            links = "".join(
                f'<code title="流程条款">{html.escape(_flow_citation_label(str(cid)))}</code>'
                for cid in citations
            )
            citation_html = (
                '<details class="w8-step-cites"><summary>'
                f"依据 {len(citations)} 条</summary>"
                f'<div class="w8-cite-links">{links}</div>'
                '<p class="w8-note">流程条款仅提供条款定位，无逐字 quote。</p>'
                "</details>"
            )
        cards.append(
            f'<article class="w8-step w8-step-card-{status}">'
            '<div class="w8-step-head">'
            f'<span class="w8-step-index">{index:02d}</span>'
            f'<h4 class="w8-step-title">{html.escape(str(field(item, "title", "")))}</h4>'
            f'<span class="w8-step-status">{_status_badge(status)}</span>'
            "</div>"
            f'<p class="w8-step-desc">{html.escape(str(field(item, "description", "")))}</p>'
            f'<div class="w8-step-meta">{"".join(meta_lines)}</div>'
            f"{material_html}{note_html}{citation_html}</article>"
        )

    if not cards:
        cards.append(_empty_state("该筛选下没有步骤", "换一个状态筛选，或勾选“显示不适用项”。"))
    return summary + "".join(cards)


def _citation_key(item: Any) -> tuple[str, int, int, int]:
    return (
        str(field(item, "article_id", "")),
        int(field(item, "part_no", 1) or 1),
        int(field(item, "quote_start", 0) or 0),
        int(field(item, "quote_end", 0) or 0),
    )


def _citation_title(item: Any) -> str:
    title = str(field(item, "doc_title", "") or field(item, "article_id", ""))
    article_no = str(field(item, "article_no", "") or "")
    return f"{title} {article_no}".strip()


def _citation_block_label(block_type: Any) -> str:
    value = str(block_type or "article")
    return {"article": "条款", "section": "章节/段落"}.get(value, value)


def citations_html(reply: Any) -> str:
    if reply is None:
        return _empty_state("还没有引用", "政策结论会附带逐字原文；流程依据会单独标注“流程条款”。")
    claims = list(field(reply, "claims", []) or [])
    claim_citations = list(field(reply, "citations", []) or [])
    if not claim_citations and claims:
        derived: list[Any] = []
        seen: set[tuple[str, int, int, int]] = set()
        for claim in claims:
            for citation in field(claim, "citations", []) or []:
                key = _citation_key(citation)
                if key not in seen:
                    seen.add(key)
                    derived.append(citation)
        claim_citations = derived

    flow_ids = _unique_flow_citations(list(field(reply, "checklist", []) or []))
    claim_ids = {str(field(item, "article_id", "")) for item in claim_citations}
    flow_ids = [article_id for article_id in flow_ids if article_id not in claim_ids]

    if not claim_citations and not flow_ids:
        outcome = str(field(reply, "outcome", "") or "")
        title = "等待补充信息" if outcome == "CLARIFY" else "本轮没有直接引用"
        text = (
            "澄清轮尚未检索条款；回答关键信息后会生成清单与依据。"
            if outcome == "CLARIFY"
            else "拒答或纯流程轮可能没有结论级引用；官方入口在对话区。"
        )
        return _empty_state(title, text)

    card_index: dict[tuple[str, int, int, int], int] = {}
    parts: list[str] = []
    if claims and claim_citations:
        for idx, citation in enumerate(claim_citations):
            card_index[_citation_key(citation)] = idx
        claim_items = []
        for claim in claims:
            chips = []
            for citation in field(claim, "citations", []) or []:
                idx = card_index.get(_citation_key(citation))
                label = _citation_title(citation)
                if idx is None:
                    chips.append(f'<span class="w8-cite-chip">{html.escape(label)}</span>')
                else:
                    chips.append(
                        f'<a class="w8-cite-chip" href="#w8-cite-{idx}">{html.escape(label)}</a>'
                    )
            claim_items.append(
                f"<li>{html.escape(str(field(claim, 'text', '')))} {''.join(chips)}</li>"
            )
        parts.append(
            f'<div class="w8-claim-map"><h4>结论到依据</h4><ol>{"".join(claim_items)}</ol></div>'
        )
        parts.append('<div class="w8-cite-note">结论引用：原文已逐字定位；来源直连学校官网。</div>')
        parts.append(
            f'<div class="w8-cite-group-title">政策结论引用'
            f"<span>{len(claim_citations)}</span></div>"
        )
        for idx, citation in enumerate(claim_citations):
            title = html.escape(_citation_title(citation))
            quote = html.escape(str(field(citation, "quote", "") or ""))
            url = str(field(citation, "source_url", "") or "")
            low = str(field(citation, "date_confidence", "high")) == "low"
            tags = [
                f'<span class="w8-cite-tag">{html.escape(_citation_block_label(field(citation, "block_type")))}</span>'
            ]
            if low:
                tags.append('<span class="w8-cite-tag w8-cite-tag-low">日期口径待核</span>')
            link = (
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener">学校官网 ↗</a>'
                if url
                else ""
            )
            parts.append(
                f'<article class="w8-cite-card" id="w8-cite-{idx}">'
                '<div class="w8-cite-head">'
                f'<h4 class="w8-cite-title">{title}</h4>'
                f'<span class="w8-cite-tags">{"".join(tags)}</span></div>'
                f'<blockquote class="w8-quote">{quote}</blockquote>'
                '<div class="w8-cite-foot">'
                f"{link}<span>原文已逐字定位</span></div></article>"
            )

    if flow_ids:
        parts.append(f'<div class="w8-cite-group-title">流程依据<span>{len(flow_ids)}</span></div>')
        parts.append(
            '<div class="w8-cite-note w8-cite-note-amber">'
            "流程清单只提供条款定位，没有逐字原文；这里不伪装成引用。</div>"
        )
        for idx, article_id in enumerate(flow_ids):
            doc_id, article_no = _flow_citation_parts(article_id)
            parts.append(
                f'<article class="w8-cite-card w8-cite-flow" id="w8-flow-cite-{idx}">'
                '<div class="w8-cite-head">'
                f'<h4 class="w8-cite-title">{html.escape(doc_id)} {html.escape(article_no)}</h4>'
                '<span class="w8-cite-tags"><span class="w8-cite-tag">流程条款</span></span>'
                "</div>"
                '<blockquote class="w8-quote w8-quote-empty">'
                "流程条款仅提供条款定位，无逐字 quote。</blockquote>"
                '<div class="w8-cite-foot"><span>无逐字 quote</span></div>'
                "</article>"
            )
    return "".join(parts)


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
    "password",
    "text_raw_ref",
    "system_prompt",
    "prompt_text",
    "mcp_payload",
}


def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                continue
            cleaned[str(key)] = _safe_payload(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value, max_chars=300)
    return _plain(value)


def _trace_node(trace: list[Any], name: str) -> dict[str, Any]:
    for item in reversed(trace):
        if str(field(item, "node", "")) == name:
            return item if isinstance(item, dict) else {}
    return {}


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(
        f"<dt>{html.escape(str(key))}</dt><dd>{html.escape(str(value if value is not None else '—'))}</dd>"
        for key, value in rows
        if value is not None
    )
    return f'<dl class="w8-kv">{body}</dl>'


def _debug_section(title: str, body: str) -> str:
    return (
        '<details class="w8-debug-section">'
        f"<summary>{html.escape(title)}<span>›</span></summary>"
        f'<div class="w8-debug-body">{body}</div></details>'
    )


def debug_html(
    reply: Any,
    *,
    metadata: dict[str, Any] | None = None,
    trace_id: str = "",
) -> str:
    if reply is None:
        return _empty_state(
            "还没有调试数据",
            "跑完一轮后显示节点耗时、Planner 来源、检索摘要、核对结果与 trace_id；默认折叠。",
        )
    metadata = metadata or {}
    trace = _safe_payload(list(field(reply, "trace", []) or []))
    planner = _trace_node(trace, "planner")
    retriever = _trace_node(trace, "retriever")
    verifier = _trace_node(trace, "verifier")
    finalize = _trace_node(trace, "finalize")
    total_ms = reply_duration_ms(reply)
    code = str(field(reply, "outcome", "") or "")
    decision = str(field(reply, "decision", "") or "—")

    planner_plan = planner.get("plan") if isinstance(planner.get("plan"), dict) else {}
    queries = planner_plan.get("queries") or retriever.get("queries") or []
    warning_values = list(field(reply, "warnings", []) or [])
    verifier_semantic = verifier.get("semantic") or {}
    semantic_labels = ""
    if isinstance(verifier_semantic, dict):
        labels = verifier_semantic.get("labels") or {}
        if isinstance(labels, dict):
            semantic_labels = "；".join(f"{k}: {v}" for k, v in labels.items())
    if not semantic_labels:
        semantic_labels = str(verifier_semantic or "—")

    summary = (
        '<div class="w8-debug-banner">'
        "已脱敏：不显示 API key、.env、完整系统提示词、用户隐私原文、"
        "text_raw_ref 或原始 MCP payload。</div>"
        '<div class="w8-debug-grid">'
        f"<div><span>outcome</span><b>{html.escape(code or '—')}</b></div>"
        f"<div><span>decision</span><b>{html.escape(decision)}</b></div>"
        f"<div><span>总耗时</span><b>{html.escape(format_duration(total_ms))}（示例）</b></div>"
        f"<div><span>trace_id</span><b>{html.escape(trace_id or '—')}</b></div>"
        "</div>"
    )

    planner_body = _kv_table(
        [
            ("planner_source", planner_plan.get("planner_source") or field(reply, "outcome", "")),
            ("intent", planner_plan.get("intent")),
            ("flow_id", field(reply, "flow_id") or planner_plan.get("flow_id")),
            ("queries", "；".join(str(q) for q in queries) if queries else "—"),
            (
                "missing_slots",
                "、".join(slot_label(str(s)) for s in (field(reply, "missing_slots", []) or []))
                or "—",
            ),
            ("clarification", field(reply, "clarification") or "—"),
            ("warnings", "；".join(html.escape(str(w)) for w in warning_values) or "—"),
        ]
    )
    retrieval_body = (
        _kv_table(
            [
                ("queries", "；".join(str(q) for q in (retriever.get("queries") or [])) or "—"),
                ("evidence_count", retriever.get("evidence", "—")),
                ("coverage", retriever.get("coverage", "—")),
                ("index_version", retriever.get("index_version") or metadata.get("index_version")),
                ("warnings", "；".join(str(w) for w in (retriever.get("warnings") or [])) or "—"),
            ]
        )
        + '<p class="w8-note">ConversationReply.trace 只提供检索摘要；逐条证据分数留在后端 trace。</p>'
    )
    verifier_body = _kv_table(
        [
            ("decision", verifier.get("decision") or decision),
            (
                "failed_claim_ids",
                "、".join(str(x) for x in (verifier.get("failed_claim_ids") or [])) or "—",
            ),
            ("semantic", semantic_labels),
            ("detail", verifier.get("detail") or "—"),
        ]
    )
    finalize_body = _kv_table(
        [
            ("outcome", finalize.get("outcome") or code),
            ("citations", finalize.get("citations", len(field(reply, "citations", []) or []))),
            ("checklist", finalize.get("checklist", len(field(reply, "checklist", []) or []))),
            ("handoff", (field(reply, "handoff", {}) or {}).get("department", "—")),
        ]
    )
    timing_rows = [
        (str(field(item, "node", "")), format_duration(field(item, "duration_ms", 0)))
        for item in trace
    ]
    timing_body = _kv_table(timing_rows) if timing_rows else '<p class="w8-note">无节点耗时。</p>'
    version_body = _kv_table(
        [
            ("model_profile", metadata.get("model_profile")),
            ("models", metadata.get("models")),
            ("prompts", metadata.get("prompt_versions")),
            ("index_version", metadata.get("index_version")),
            ("graph_version", metadata.get("graph_version")),
            ("trace_id", trace_id),
        ]
    )
    raw_body = (
        '<details class="w8-raw"><summary>原始 trace（已脱敏）</summary><pre>'
        f"{html.escape(json.dumps(trace, ensure_ascii=False, indent=2))}</pre></details>"
    )
    return (
        summary
        + _debug_section("Planner", planner_body)
        + _debug_section("Retrieval", retrieval_body)
        + _debug_section("Verifier", verifier_body)
        + _debug_section("Finalize", finalize_body)
        + _debug_section("节点耗时", timing_body)
        + _debug_section("模型与版本", version_body)
        + raw_body
    )


def clarify_options(reply: Any) -> list[tuple[str, str]]:
    if reply is None or str(field(reply, "outcome", "")) != "CLARIFY":
        return []
    clarification = field(reply, "clarification") or {}
    allowed = list(field(clarification, "allowed_values", []) or [])
    return [(value_label(str(value)), str(value)) for value in allowed]


def clarify_prompt_html(reply: Any) -> str:
    if reply is None or str(field(reply, "outcome", "")) != "CLARIFY":
        return ""
    clarification = field(reply, "clarification") or {}
    question = str(field(clarification, "question", "") or field(reply, "text", "") or "")
    missing = list(
        field(clarification, "missing_slots", []) or field(reply, "missing_slots", []) or []
    )
    labels = "、".join(slot_label(str(slot)) for slot in missing) or "下一项信息"
    return (
        '<div class="w8-clarify-prompt">'
        f"<strong>{html.escape(question)}</strong>"
        f"<span>还缺：{html.escape(labels)}</span></div>"
    )


def conversation_header_html(
    *,
    title: str,
    meta: str,
    outcome: str | None,
    closed: bool,
    profile: str,
) -> str:
    badges = outcome_badge_html(outcome, closed=closed) if outcome else ""
    # 不在主界面标注模型：评测与生产模型分离，模型信息只在评测报告和调试面板中记录。
    _ = profile
    return (
        '<div class="w8-conversation-head">'
        '<div class="w8-conversation-title">'
        f"<h2>{html.escape(title)}</h2><p>{html.escape(meta)}</p></div>"
        f'<div class="w8-conversation-badges">{badges}</div>'
        "</div>"
    )


def side_header_html(reply: Any, *, title: str = "", sub: str = "") -> str:
    if reply is None:
        return (
            '<div class="w8-side-header"><div class="w8-side-kicker">最后一轮</div>'
            "<h3>等待提问</h3><p>选择一段对话查看办理清单、引用与调试</p></div>"
        )
    code = str(field(reply, "outcome", "") or "")
    meta = outcome_meta(code)
    closed = bool(field(reply, "closed", False))
    badges = outcome_badge_html(code, closed=closed)
    heading = title or meta["title"]
    subtitle = sub or str(field(reply, "flow_id", "") or meta["title"])
    return (
        '<div class="w8-side-header">'
        '<div class="w8-side-kicker">最后一轮</div>'
        f'<div class="w8-side-badges">{badges}</div>'
        f"<h3>{html.escape(heading)}</h3>"
        f"<p>{html.escape(subtitle)}</p></div>"
    )

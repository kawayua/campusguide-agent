"""W4 单轮 Gradio 页面：MCP 混合检索 + W3 引用核对/拒答。"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import gradio as gr

from campusguide.config import load_organization
from campusguide.domain.enums import Audience, AudienceStage
from campusguide.domain.profiles import UserProfile
from campusguide.env import load_dotenv
from campusguide.qa.finalize import LOW_DATE_NOTICE, citation_label
from campusguide.qa.runtime import ROOT, create_mcp_qa_service
from campusguide.qa.service import QAResult, QAService

AUDIENCES = {
    "未指定": None,
    "本科生": Audience.UNDERGRADUATE,
    "研究生": Audience.GRADUATE,
    "教师": Audience.FACULTY,
}
STAGES = {
    "未指定": None,
    "大一": AudienceStage.YEAR1,
    "大二": AudienceStage.YEAR2,
    "大三": AudienceStage.YEAR3,
    "大四": AudienceStage.YEAR4,
    "最后一年": AudienceStage.FINAL_YEAR,
}

_state: dict[str, Any] = {"service": None, "error": None}


def _organization_choices() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    try:
        organization = load_organization(ROOT / "configs" / "organization.yaml")
    except Exception:
        return [("未指定", "")], [("未指定", "")]
    colleges = [("未指定", "")] + [
        (f"{college.name}（{college.college_id}）", college.college_id)
        for college in organization.colleges
    ]
    majors = [("未指定", "")] + [
        (f"{major.name}（{major.major_id}）", major.major_id) for major in organization.majors
    ]
    return colleges, majors


def _get_service() -> QAService:
    if _state["service"] is not None:
        return _state["service"]
    if _state["error"] is not None:
        raise RuntimeError(_state["error"])
    try:
        # 启动时拉起 policy-search/policy-meta；模型加载在 server 内完成。
        service = create_mcp_qa_service()
        service.warmup()
        _state["service"] = service
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(_state["error"]) from exc
    return _state["service"]


def _citation_cards(result: QAResult) -> str:
    if not result.citations:
        return "_无引用卡片_"
    lines: list[str] = []
    for index, citation in enumerate(result.citations, start=1):
        lines.append(f"**{index}. {citation_label(citation)}**")
        lines.append("")
        lines.append(f"- 块类型：`{citation.block_type.value}`")
        lines.append(f"- 来源：[{citation.source_url}]({citation.source_url})")
        if citation.date_confidence == "low":
            lines.append(f"- 日期提示：{LOW_DATE_NOTICE}")
        quote = citation.quote.strip().replace("\n", "\n> ")
        lines.extend(["", f"> {quote}", ""])
    return "\n".join(lines)


def _debug_panel(result: QAResult) -> str:
    retrieval = [
        {
            "rank": item.get("rank"),
            "article_id": item.get("article_id"),
            "part_no": item.get("part_no"),
            "block_type": item.get("block_type"),
            "dense": item.get("dense_score"),
            "sparse": item.get("sparse_score"),
            "rrf": item.get("rrf_score"),
            "rerank": item.get("rerank_score"),
        }
        for item in result.retrieval_scores
    ]
    payload = {
        "outcome": result.outcome.value,
        "decision": result.decision.value if result.decision else None,
        "partial": result.partial,
        "trace_id": result.trace_id,
        "duration_ms": result.duration_ms,
        "probe_ms": result.probe_ms,
        "qa_ms": result.qa_ms,
        "queries": result.queries,
        "index_version": result.index_version,
        "mcp": result.mcp,
        "retrieval_timing": result.retrieval_timing,
        "retrieval_warnings": result.retrieval_warnings,
        "version_metadata": result.version_metadata,
        "llm_model": result.llm_model,
        "retrieval": retrieval,
        "rendered_claim_ids": result.rendered_claim_ids,
        "dropped_claim_ids": result.dropped_claim_ids,
        "issues": [issue.model_dump(mode="json", exclude_none=True) for issue in result.issues],
        "scope_issues": [
            issue.model_dump(mode="json", exclude_none=True) for issue in result.scope_issues
        ],
        "repealed_probe": result.probe,
        "dropped_claims": result.dropped_claims,
        "dropped_citations": result.dropped_citations,
        "reason": result.message,
    }
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def answer_question(
    question: str,
    audience_label: str,
    college: str,
    major: str,
    intake_year: float | int | None,
    stage_label: str,
) -> tuple[str, str, str]:
    question = (question or "").strip()
    if not question:
        return "请输入问题。", "_无引用卡片_", '```json\n{"outcome": "INVALID_REQUEST"}\n```'
    try:
        service = _get_service()
    except RuntimeError as exc:
        message = (
            f"**服务未就绪**\n\n{exc}\n\n"
            "请先启动 Qdrant、运行 `build_vectors.py`，并在 `.env` 中配置 "
            "`LLM_BASE_URL` / `LLM_API_KEY` / `ADVISOR_MODEL`。"
        )
        return message, "_无引用卡片_", f"```\n{exc}\n```"

    audience = AUDIENCES.get(audience_label)
    profile = UserProfile(
        colleges=[college] if college else [],
        majors=[major] if major else [],
        intake_year=int(intake_year) if intake_year else None,
        stage=STAGES.get(stage_label),
    )
    try:
        result = service.ask(
            question,
            audience=audience,
            profile=profile if not profile.is_empty else None,
        )
    except Exception as exc:
        return (
            f"**请求失败**\n\n`{type(exc).__name__}: {exc}`",
            "_无引用卡片_",
            f"```\n{type(exc).__name__}: {exc}\n```",
        )

    if result.renders_answer:
        answer = result.answer_markdown
    else:
        answer = f"**{result.outcome.value}**\n\n{result.message}"
    return answer, _citation_cards(result), _debug_panel(result)


def build_demo() -> gr.Blocks:
    college_choices, major_choices = _organization_choices()
    with gr.Blocks(title="CampusGuide W4 单轮问答") as demo:
        gr.Markdown(
            "# CampusGuide 单轮制度问答\n"
            "W4：检索经 `policy-search` MCP（bge-m3 dense + BM25 sparse + RRF + "
            "bge-reranker），答案仍由 W3 确定性核对；未通过的 claim 会被省略。"
        )
        with gr.Row():
            question = gr.Textbox(
                label="问题",
                placeholder="例如：申请国家奖学金需要满足哪些条件？",
                lines=2,
                scale=4,
            )
            audience = gr.Dropdown(
                choices=list(AUDIENCES),
                value="未指定",
                label="受众",
                scale=1,
            )
        with (
            gr.Accordion("可选：人群信息（用于提前过滤不适用的条款）", open=False),
            gr.Row(),
        ):
            college = gr.Dropdown(choices=college_choices, value="", label="学院")
            major = gr.Dropdown(choices=major_choices, value="", label="专业")
            intake_year = gr.Number(label="入学年份", precision=0)
            stage = gr.Dropdown(choices=list(STAGES), value="未指定", label="阶段")
        submit = gr.Button("提问", variant="primary")
        with gr.Row():
            with gr.Column(scale=3):
                answer = gr.Markdown(label="答案")
                citations = gr.Markdown(label="引用卡片")
            with gr.Column(scale=2), gr.Accordion("调试面板", open=False):
                debug = gr.Markdown()
        inputs = [question, audience, college, major, intake_year, stage]
        outputs = [answer, citations, debug]
        submit.click(answer_question, inputs=inputs, outputs=outputs)
        question.submit(answer_question, inputs=inputs, outputs=outputs)
        gr.Markdown(
            "> 演示系统不构成官方答复，具体以学校官网为准。"
            "W4 只渲染通过确定性核对的 claim；部分作答不计入完整回答。"
        )
    return demo


def main() -> None:
    load_dotenv()
    with contextlib.suppress(RuntimeError):
        # 启动检查失败不阻塞页面；错误会显示在首次提问的回答区。
        _get_service()
    demo = build_demo()
    demo.launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
    )


if __name__ == "__main__":
    main()

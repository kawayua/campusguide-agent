"""W2 最小 Gradio 单轮页面。"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import gradio as gr

from campusguide.domain.enums import Audience
from campusguide.env import load_dotenv
from campusguide.qa.finalize import LOW_DATE_NOTICE, citation_label
from campusguide.qa.runtime import create_qa_service, create_qdrant_client
from campusguide.qa.service import QAResult, QAService

AUDIENCES = {
    "未指定": None,
    "本科生": Audience.UNDERGRADUATE,
    "研究生": Audience.GRADUATE,
    "教师": Audience.FACULTY,
}

_state: dict[str, Any] = {"service": None, "error": None}


def _get_service() -> QAService:
    if _state["service"] is not None:
        return _state["service"]
    if _state["error"] is not None:
        raise RuntimeError(_state["error"])
    try:
        client = create_qdrant_client()
        service = create_qa_service(client=client)
        # 启动时预加载 bge-m3，避免首个提问承担 10s+ 的冷启动。
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
            "score": item.get("score"),
        }
        for item in result.retrieval_scores
    ]
    payload = {
        "outcome": result.outcome.value,
        "trace_id": result.trace_id,
        "duration_ms": result.duration_ms,
        "queries": result.queries,
        "llm_model": result.llm_model,
        "retrieval": retrieval,
        "dropped_claims": result.dropped_claims,
        "dropped_citations": result.dropped_citations,
        "reason": result.message,
    }
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def answer_question(question: str, audience_label: str) -> tuple[str, str, str]:
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
    try:
        result = service.ask(question, audience=audience)
    except Exception as exc:
        return (
            f"**请求失败**\n\n`{type(exc).__name__}: {exc}`",
            "_无引用卡片_",
            f"```\n{type(exc).__name__}: {exc}\n```",
        )

    if result.answered:
        answer = result.answer_markdown
    else:
        answer = f"**{result.outcome.value}**\n\n{result.message}"
    return answer, _citation_cards(result), _debug_panel(result)


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="CampusGuide W2 单轮问答") as demo:
        gr.Markdown(
            "# CampusGuide 单轮制度问答\n"
            "W2 垂直切片：只回答现行 `article` 条款；答案中的每条结论都必须能回链到本轮证据。"
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
        submit = gr.Button("提问", variant="primary")
        with gr.Row():
            with gr.Column(scale=3):
                answer = gr.Markdown(label="答案")
                citations = gr.Markdown(label="引用卡片")
            with gr.Column(scale=2), gr.Accordion("调试面板", open=False):
                debug = gr.Markdown()
        submit.click(
            answer_question,
            inputs=[question, audience],
            outputs=[answer, citations, debug],
        )
        question.submit(
            answer_question,
            inputs=[question, audience],
            outputs=[answer, citations, debug],
        )
        gr.Markdown(
            "> 演示系统不构成官方答复，具体以学校官网为准。"
            "W2 不报告答案正确率；引用 ID 与 quote 可定位是本周硬门禁。"
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

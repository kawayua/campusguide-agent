"""Advisor：claim-first 生成。

模型只输出 claims JSON；正文由 Finalize 渲染，避免出现未被引用覆盖的句子。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from campusguide.agents.providers import ChatProvider, ChatProviderError, ChatResponse
from campusguide.domain.enums import Audience, ClaimKind
from campusguide.domain.models import Evidence


class AdvisorError(RuntimeError):
    """模型输出不可解析，或重试后仍失败。"""


class CitationDraft(BaseModel):
    article_id: str = ""
    quote: str = ""


class ClaimDraft(BaseModel):
    claim_id: str | None = None
    text: str = ""
    kind: str = "other"
    citations: list[CitationDraft] = Field(default_factory=list)


class ClaimsDraft(BaseModel):
    claims: list[ClaimDraft] = Field(default_factory=list)


@dataclass
class AdvisorResult:
    drafts: list[ClaimDraft]
    responses: list[ChatResponse] = field(default_factory=list)
    repaired: bool = False


SYSTEM_PROMPT = """你是华南农业大学制度问答助手。你只能依据本轮提供的“证据片段”回答。
必须遵守：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。
2. JSON 结构：{"claims": [{"claim_id": "c1", "text": "...", "kind": "eligibility", "citations": [{"article_id": "...", "quote": "..."}]}]}。
3. 生成 1-5 条 claim；每条 claim 只能包含一个事实性结论，并至少包含 1 个 citation。
4. citation.article_id 必须逐字取自证据中的“引用ID”，不得编造、不得改写。
5. citation.quote 必须从对应证据的“正文”中逐字复制一段连续文字，至少 6 个字；
   不得改写、不得使用省略号、不得拼接不相邻的句子。
6. 不得使用证据之外的知识；证据不足时返回 {"claims": []}。
7. 不要输出“根据规定”“综上所述”等无信息量句子；不要把多个条件合并成一条 claim。
8. kind 只能取 eligibility / step / material / deadline / department / version / definition / other。"""


def _audience_label(audience: Audience | None) -> str:
    if audience is None:
        return "未指定"
    return {
        Audience.UNDERGRADUATE: "本科生",
        Audience.GRADUATE: "研究生",
        Audience.GRADUATE_MASTER: "硕士研究生",
        Audience.GRADUATE_DOCTOR: "博士研究生",
        Audience.FACULTY: "教师",
        Audience.STAFF: "教职工",
        Audience.PUBLIC: "公众",
        Audience.ALL: "全体",
    }.get(audience, audience.value)


def build_evidence_block(evidence: list[Evidence]) -> str:
    blocks: list[str] = []
    for item in evidence:
        article = item.article
        location = "章节/段落" if article.block_type.value != "article" else article.article_no
        confidence = "（日期为锚点，非精确发布日）" if article.date_confidence == "low" else ""
        blocks.append(
            "\n".join(
                [
                    f"[证据{item.rank}]",
                    f"引用ID：{article.article_id}",
                    f"文档：{article.doc_title}",
                    f"位置：{location}（{article.block_type.value}）",
                    f"生效日期：{article.effective_from.isoformat()}{confidence}",
                    "正文：",
                    article.text,
                ]
            )
        )
    return "\n\n".join(blocks)


def build_messages(
    question: str,
    evidence: list[Evidence],
    audience: Audience | None,
) -> list[dict[str, str]]:
    user = (
        f"受众：{_audience_label(audience)}\n"
        f"问题：{question}\n\n"
        f"证据片段：\n{build_evidence_block(evidence)}\n\n"
        "请只输出符合 schema 的 claims JSON。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _extract_json_text(content: str) -> str:
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return text


def parse_claims_json(content: str) -> ClaimsDraft:
    if not content.strip():
        raise AdvisorError("模型返回空内容")
    text = _extract_json_text(content)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdvisorError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict) or "claims" not in payload:
        raise AdvisorError('模型输出缺少顶层 "claims" 字段')
    if not isinstance(payload["claims"], list):
        raise AdvisorError('"claims" 必须是数组')
    claims: list[ClaimDraft] = []
    for item in payload["claims"]:
        if not isinstance(item, dict):
            raise AdvisorError("claims 数组元素必须是对象")
        citations = item.get("citations") or []
        if not isinstance(citations, list):
            raise AdvisorError("citations 必须是数组")
        claims.append(
            ClaimDraft(
                claim_id=item.get("claim_id") if isinstance(item.get("claim_id"), str) else None,
                text=item.get("text") if isinstance(item.get("text"), str) else "",
                kind=item.get("kind") if isinstance(item.get("kind"), str) else "other",
                citations=[
                    CitationDraft(
                        article_id=(
                            citation.get("article_id")
                            if isinstance(citation, dict)
                            and isinstance(citation.get("article_id"), str)
                            else ""
                        ),
                        quote=(
                            citation.get("quote")
                            if isinstance(citation, dict) and isinstance(citation.get("quote"), str)
                            else ""
                        ),
                    )
                    for citation in citations
                ],
            )
        )
    return ClaimsDraft(claims=claims)


def normalize_kind(value: str) -> ClaimKind:
    try:
        return ClaimKind(value)
    except ValueError:
        return ClaimKind.OTHER


class Advisor:
    def __init__(
        self,
        provider: ChatProvider,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    def generate(
        self,
        question: str,
        evidence: list[Evidence],
        audience: Audience | None = None,
    ) -> AdvisorResult:
        messages = build_messages(question, evidence, audience)
        responses: list[ChatResponse] = []
        try:
            first = self._provider.complete(
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except ChatProviderError:
            raise
        responses.append(first)
        try:
            return AdvisorResult(
                drafts=parse_claims_json(first.content).claims, responses=responses
            )
        except AdvisorError as first_error:
            repair_messages: list[dict[str, Any]] = [
                *messages,
                {"role": "assistant", "content": first.content},
                {
                    "role": "user",
                    "content": (
                        "上面的输出无法解析为约定 JSON："
                        f"{first_error}。请只重新输出一个合法 JSON 对象，"
                        '结构为 {"claims": [...]}，不要代码块，不要解释。'
                    ),
                },
            ]
            try:
                second = self._provider.complete(
                    repair_messages,
                    temperature=0.0,
                    max_tokens=self._max_tokens,
                )
            except ChatProviderError:
                raise
            responses.append(second)
            try:
                drafts = parse_claims_json(second.content).claims
            except AdvisorError as second_error:
                raise AdvisorError(f"JSON 修复失败：{second_error}") from second_error
            return AdvisorResult(drafts=drafts, responses=responses, repaired=True)

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
from campusguide.agents.quote_repair import (
    build_repair_messages,
    find_quote_problems,
    merge_repairs,
    remaining_problem_ids,
)
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
    quote_repair_attempted: bool = False
    quote_repaired: bool = False
    quote_repair_remaining: list[str] = field(default_factory=list)
    quote_repair_error: str = ""


PROMPT_VERSION = "w6a2-2026-10-24"

SYSTEM_PROMPT = """你是华南农业大学制度问答助手。你只能依据本轮提供的“证据片段”回答。
必须遵守：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。
2. JSON 结构：{"claims": [{"claim_id": "c1", "text": "...", "kind": "eligibility", "citations": [{"article_id": "...", "quote": "..."}]}]}。
3. 生成 1-5 条 claim；每条 claim 只能包含一个事实性结论，并至少包含 1 个 citation。
4. citation.article_id 必须逐字取自证据中的“引用ID”，不得编造、不得改写。
5. citation.quote 必须从对应证据的“正文”中逐字复制**一个连续子串**，至少 6 个字；
   不得改写、不得使用省略号、不得拼接不相邻的句子。
   quote 必须包含 claim 的完整“条件 + 后果/结论”；不能只截取条件、只截取后果。
   如果结论在引导句、分项在下方，必须从引导句连续复制到该分项，允许包含中间分项；
   绝不能用省略号或跳过中间内容把“引导句 + 非相邻分项”拼成一段。
   无法用连续 quote 支持时，缩短 claim、删除该 claim 或返回空 claims，不要拼接。
6. claim 只能复述其 citation.quote 已经写出的事实。不得补充 quote 未出现的
   条件、范围、时间、地点、受众、后果、版本号、条号或评价性概括；
   不得用“问题”中的词补足 quote 没有的内容。
   claim 中的时间/范围条件（如“一学期内”“每学年”）和数字、金额、期限必须原样
   出现在 quote 中；quote 没有写出的范围/数字，claim 也不能写。
7. 保留原文的限定词和强弱：一般、原则上、可以、应当、不得、至少、最多等；
   不得把“可以/一般”改写成“必须/一律”，也不得反向弱化。
8. 如果 claim 需要列举多项，quote 必须包含这些项的原文；否则不要展开具体内容，
   只写 quote 明确写出的引用关系（如“应同时符合条件（一）至（五）”）。
9. 不得使用证据之外的知识；证据不足时返回 {"claims": []}。
10. 一条 claim 只表达一个条件、一个后果或一个数量；不要把多个条件或处分档次
    合并成一条 claim；一条 claim 尽量只保留一个条件及其直接后果。
    不要展开列表或概括条号；引用关系只能写到 quote 已明确写出的范围。
11. 输出前逐条自检：claim 中每个主体、条件、数字/日期/金额/期限、后果和
    限定词是否都能在 quote 中找到？找不到就删掉该部分、缩短 claim 或改选
    更长的 quote；不要用条款上下文或其他证据补足。
12. kind 只能取 eligibility / step / material / deadline / department / version / definition / other。
13. 如果找不到能同时覆盖“条件 + 后果”的连续 quote，不要生成该 claim；
    不要用两个不相邻 quote、省略号或“（一）至（五）”之类概括来掩盖缺失。"""


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
        "请先为每条 claim 选择完整的 citation.quote，再写 claim；"
        "quote 必须是同一证据正文中的连续子串；不得用省略号、不得跳过中间分项，"
        "也不得把“引导句 + 非相邻分项”拼接起来。"
        "claim 的时间/范围条件和数字/金额/期限必须原样出现在 quote 中。"
        "一条 claim 只表达一个条件、一个后果或一个数量；"
        "claim 不得超出 quote。quote 不完整时请缩短 claim、删除该 claim 或返回空 claims。\n"
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
        enable_quote_repair: bool = True,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._enable_quote_repair = enable_quote_repair

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
        json_repaired = False
        try:
            drafts = parse_claims_json(first.content).claims
            draft_content = first.content
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
            draft_content = second.content
            json_repaired = True

        return self._repair_quotes(
            messages,
            draft_content,
            drafts,
            evidence,
            responses,
            json_repaired=json_repaired,
        )

    def _repair_quotes(
        self,
        messages: list[dict[str, str]],
        draft_content: str,
        drafts: list[ClaimDraft],
        evidence: list[Evidence],
        responses: list[ChatResponse],
        *,
        json_repaired: bool,
    ) -> AdvisorResult:
        result = AdvisorResult(
            drafts=drafts,
            responses=responses,
            repaired=json_repaired,
        )
        if not self._enable_quote_repair:
            return result
        problems = find_quote_problems(drafts, evidence)
        if not problems:
            return result

        result.quote_repair_attempted = True
        repair_messages = build_repair_messages(messages, draft_content, drafts, problems)
        try:
            repaired_response = self._provider.complete(
                repair_messages,
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except ChatProviderError as exc:
            # 修复调用失败不阻塞整轮；保留原 draft，让 Verifier 按契约丢弃。
            result.quote_repair_error = f"{type(exc).__name__}: {exc}"
            return result
        responses.append(repaired_response)
        try:
            repaired_drafts = parse_claims_json(repaired_response.content).claims
        except AdvisorError as exc:
            result.quote_repair_error = str(exc)
            return result

        merged = merge_repairs(drafts, repaired_drafts, problems)
        result.drafts = merged.drafts
        result.quote_repaired = merged.changed
        result.quote_repair_remaining = remaining_problem_ids(merged.drafts, evidence)
        return result

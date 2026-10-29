"""claim-quote 语义核对（W6 第二层）。

只判断“quote 是否支持 claim”，不回答用户、不引入证据外知识。模型与 Advisor
使用不同系统提示词、不同输入（只有 claim + quote），温度 0；JSON 非法时
修复一次，仍失败按 unavailable 处理，由图决定 strict 拒答或显式标注。
"""

from __future__ import annotations

import json
import re
import time
from enum import StrEnum

from pydantic import BaseModel

from campusguide.agents.providers import ChatProvider, ChatProviderError
from campusguide.domain.models import Claim, Evidence
from campusguide.domain.source_context import trusted_context
from campusguide.observability.errors import safe_error_summary

SEMANTIC_PROMPT_VERSION = "w6b-semantic-quality-2026-10-28"


class SemanticLabel(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"


class SemanticReport(BaseModel):
    labels: dict[str, SemanticLabel] = {}
    details: dict[str, str] = {}
    model: str = ""
    latency_ms: int = 0
    checked: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    unavailable: bool = False
    error: str = ""

    @property
    def contradicted_ids(self) -> list[str]:
        return [
            claim_id
            for claim_id, label in self.labels.items()
            if label is SemanticLabel.CONTRADICTED
        ]

    @property
    def weak_ids(self) -> list[str]:
        return [
            claim_id
            for claim_id, label in self.labels.items()
            if label in {SemanticLabel.PARTIALLY_SUPPORTED, SemanticLabel.INSUFFICIENT}
        ]


SYSTEM_PROMPT = """你是引用核对器，只判断“引文是否支持结论”，不回答用户问题。
对每个 claim/quote 对输出一个标签：
- supported：quote 完整支持 claim 的主体、条件、范围、后果和数字；
- partially_supported：quote 只支持一部分，或缺少 claim 写出的条件/范围/例外；
- contradicted：quote 与 claim 冲突（包括否定、范围相反、数字/日期不同）；
- insufficient：quote 不足以判断 claim 是否成立。
必须保留原文限定词（一般/原则上/可以/应当/不得/至少/最多），检查条件、范围、
例外、时间、数字和金额。
条件方向必须逐字对照：原文“30分钟后”与“30分钟内”、“前”与“后”、“以上”与“以下”、
“禁止”与“可以”不可互换；主体、动作、对象也不得调换（谁申请、谁审批、第一还是第二导师）。
列表题逐项核对实体类别：同一页出现的实验室不因并列排版就成为“示范中心”，
原文用“另设/此外/其他”引出的条目属于另一类别。
补充说明与正文冲突时，先看补充说明的适用条件与发布主体，例外不能被写成通则。可以使用article_title、article_no、trusted_source_context等可信同源元数据解释主体归属、
名单所属系、适用专业/对象与表头，不要求同一名单正文重复标题的学院名称。
名单中姓名可存在排版空格，但不得拼接两个相邻的完整姓名；必须同时有正文姓名及名册归属。
来源标题/章节不能补出正文中不存在的数字、金额、期限、课程、第一/第二导师关系或成员身份。
多个quote可各支持一个子事实，保持来源独立。不得使用外部知识或仅凭问题判断supported。
只输出 JSON：{"labels":[{"claim_id":"c1","label":"supported","detail":"..."}]}。
label 只能是 supported / partially_supported / contradicted / insufficient。"""


def _item_payload(claim: Claim, evidence: list[Evidence] | None = None) -> dict:
    quotes: list[str] = []
    title = ""
    article_no = ""
    for citation in claim.citations:
        quotes.append(citation.quote)
        if not title:
            title = citation.doc_title
            article_no = citation.article_no
    return {
        "claim_id": claim.claim_id,
        "claim": claim.text,
        "quote": "\n---\n".join(quotes),
        "article_title": title,
        "article_no": article_no,
        "trusted_source_context": [
            trusted_context(item.article)
            for item in (evidence or [])
            if any(
                c.article_id == item.article.article_id and c.part_no == item.article.part_no
                for c in claim.citations
            )
        ],
    }


class SemanticVerifier:
    def __init__(
        self,
        provider: ChatProvider | None,
        *,
        enabled: bool = True,
        batch_size: int = 5,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._provider = provider
        self._enabled = enabled
        self._batch_size = max(1, min(batch_size, 5))
        self._timeout_seconds = timeout_seconds

    def check(self, claims: list[Claim], evidence: list[Evidence]) -> SemanticReport:
        if len(claims) <= self._batch_size:
            return self._check_batch(claims, evidence)
        combined = SemanticReport()
        for start in range(0, len(claims), self._batch_size):
            report = self._check_batch(claims[start : start + self._batch_size], evidence)
            combined.labels.update(report.labels)
            combined.details.update(report.details)
            combined.latency_ms += report.latency_ms
            combined.checked += report.checked
            combined.prompt_tokens += report.prompt_tokens
            combined.completion_tokens += report.completion_tokens
            combined.model = report.model
            if report.unavailable:
                combined.unavailable = True
                combined.error = report.error
                break
        return combined

    def _check_batch(self, claims: list[Claim], evidence: list[Evidence]) -> SemanticReport:
        if not claims:
            return SemanticReport(checked=0)
        if not self._enabled:
            return SemanticReport(unavailable=True, error="语义核对未启用")
        if self._provider is None:
            return SemanticReport(unavailable=True, error="语义模型未配置")
        items = [_item_payload(claim, evidence) for claim in claims[: self._batch_size]]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请逐条判断：\n"
                + json.dumps({"items": items}, ensure_ascii=False, indent=2),
            },
        ]
        started = time.perf_counter()
        try:
            response = self._provider.complete(messages, temperature=0.0, max_tokens=1200)
        except ChatProviderError as exc:
            return SemanticReport(
                unavailable=True,
                error=safe_error_summary(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        latency = int((time.perf_counter() - started) * 1000)
        tokens_in, tokens_out = response.prompt_tokens, response.completion_tokens
        payload = _parse_payload(response.content)
        model = response.model
        if payload is None:
            repair = [
                *messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        "上面的输出不是合法 JSON。请只输出 "
                        '{"labels":[{"claim_id":"c1","label":"supported","detail":"..."}]}。'
                    ),
                },
            ]
            try:
                response = self._provider.complete(repair, temperature=0.0, max_tokens=1200)
            except ChatProviderError as exc:
                return SemanticReport(
                    unavailable=True,
                    error="repair:" + safe_error_summary(exc),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            latency = int((time.perf_counter() - started) * 1000)
            model = response.model
            tokens_in += response.prompt_tokens
            tokens_out += response.completion_tokens
            payload = _parse_payload(response.content)
            if payload is None:
                return SemanticReport(
                    unavailable=True,
                    error="语义输出不是合法 JSON（已修复一次）",
                    model=model,
                    latency_ms=latency,
                )
        labels: dict[str, SemanticLabel] = {}
        details: dict[str, str] = {}
        expected = {item["claim_id"] for item in items}
        for raw in payload.get("labels") or []:
            if not isinstance(raw, dict):
                continue
            claim_id = raw.get("claim_id")
            if not isinstance(claim_id, str) or claim_id not in expected:
                continue
            try:
                label = SemanticLabel(str(raw.get("label")))
            except ValueError:
                label = SemanticLabel.INSUFFICIENT
            labels[claim_id] = label
            details[claim_id] = str(raw.get("detail") or "")[:300]
        missing = expected - set(labels)
        for claim_id in missing:
            labels[claim_id] = SemanticLabel.INSUFFICIENT
            details[claim_id] = "语义模型未返回该条标签"
        return SemanticReport(
            labels=labels,
            details=details,
            model=model,
            latency_ms=latency,
            checked=len(labels),
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
        )


def _parse_payload(content: str) -> dict | None:
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("labels"), list):
        return None
    return payload


__all__ = [
    "SEMANTIC_PROMPT_VERSION",
    "SemanticLabel",
    "SemanticReport",
    "SemanticVerifier",
]

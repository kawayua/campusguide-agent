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
from campusguide.retrieval.source_spans import table_header_context, table_role

SEMANTIC_PROMPT_VERSION = "w6b-semantic-table-2026-10-31-entailment"


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
- supported：quote 完整支持 claim 明确写出的主体、条件、范围、后果和数字；
  quote 包含 claim 未写出且不影响 claim 成立的额外独立事实，不影响 supported；
- partially_supported：quote 只支持 claim 明确写出的一部分，或缺少 claim 写出的
  条件/范围/例外；quote 多出的、与 claim 成立无关的独立事实不构成 claim 缺漏；
- contradicted：quote 与 claim 冲突（包括否定、范围相反、数字/日期不同）；
- insufficient：quote 不足以判断 claim 是否成立。
只核对 claim 明确写出的子事实及其成立所必需的条件、限定和例外；quote 中多出的、
与 claim 成立无关的另一个独立事实、动作或期限不构成 claim 缺漏，不因此判
partially_supported。
不得仅因两个动作、期限或主体在同一 quote 中出现，就臆测 claim 表达了“同期、绑定、
因果或同一人”等未写出的关系，并据此要求补全或判缺漏。若 claim 的其它来源/quote
明确给出使其成立的关键限制或例外，仍必须纳入核对，不得以“只核 claim”忽略。
列举不同动作各自期限时，不自动视为同期或绑定；claim 明确写同期/绑定而原文不同，
必须判 contradicted。例：原文并列给出动作A期限、费用和动作B期限，claim只写动作A
期限与费用，应判 supported；claim若写A与B同期而原文不同，则应判 contradicted。
必须保留原文限定词（一般/原则上/可以/应当/不得/至少/最多），检查 claim 明确
写出的条件、范围、例外、时间、数字和金额。
条件方向必须逐字对照：原文“30分钟后”与“30分钟内”、“前”与“后”、“以上”与“以下”、
“禁止”与“可以”不可互换；主体、动作、对象也不得调换（谁申请、谁审批、第一还是第二导师）。
列表题逐项核对实体类别：同一页出现的实验室不因并列排版就成为“示范中心”，
原文用“另设/此外/其他”引出的条目属于另一类别。
补充说明与正文冲突时，先看补充说明的适用条件与发布主体，例外不能被写成通则。可以使用article_title、article_no、trusted_source_context等可信同源元数据解释主体归属、
名单所属系、适用专业/对象与表头，不要求同一名单正文重复标题的学院名称。
名单中姓名可存在排版空格，但不得拼接两个相邻的完整姓名；必须同时有正文姓名及名册归属。
来源标题/章节不能补出正文中不存在的数字、金额、期限、课程、第一/第二导师关系或成员身份。
多个quote可各支持一个子事实。sources中的每项都将引文与它自己的标题、版本和章节绑定，
跨文档/跨版本比较时逐一对应，不能把首个来源的版本或表头套给其余引文。
表头与课程行可能分属不同原始片段：table_header是独立原文，只解释本表各列的列含义，
不得与行quote拼接，也不得跨表套用。table_role=support_matrix的行只说明课程对毕业要求/
培养目标的支撑关系，不是培养计划、开设课程或学分清单。缺少表头或列语义缺失时，
不得推断学分、总学时、周数、学期，也不得把行内数字擅自对应到某一列。
原文职务与工作职责分开核对，“副院长主持行政工作”不能改成“院长”。
不得使用外部知识或仅凭问题判断supported。
只输出 JSON：{"labels":[{"claim_id":"c1","label":"supported","detail":"..."}]}。
label 只能是 supported / partially_supported / contradicted / insufficient。"""

# A negative label whose own full detail also claims consistency/support is
# internally inconsistent. This only triggers a bounded recheck; it never
# changes a label by itself.
_CONSISTENCY_HINT = re.compile(
    r"(?:与|和)(?:claim|结论|原文|引文|该条|该项)"
    r"[^。；，,？?不非未无]{0,10}(?:一致|相符|吻合)"
    r"|(?:各项|两者|二者|表述|内容)(?:均|都|完全|全部)?(?:一致|相符|吻合)"
    r"|(?<![不非未无])(?:故|因此|所以|应|应该|应当|综上)"
    r"[^。；，,？?不非未无]{0,12}(?:为|判为|判定为|认定为|视为)?\s*"
    r"(?:supported|支持)"
)

RECHECK_PROMPT = (
    "初次核对将以下条目标为否定，但说明文本自称与claim一致。"
    "请只依据每条自己的 sources 重新独立核对，不得引用其他条目的引文。"
    "只输出 JSON："
    '{"labels":[{"claim_id":"c1","label":"supported|partially_supported|'
    'contradicted|insufficient","detail":"..."}]}。'
)


_HINT_LOCAL_BOUNDARY = re.compile(r"[。；;，,？?！!]|但(?:是)?|然而|不过|而是")
_HINT_LOCAL_NEGATION = re.compile(
    r"(?:并非|并不是|不是|不能|未能|无法|不予|不得|尚未|并未|没有|不存在"
    r"|不(?!仅|但|只|单)|非|未|无|没)"
    r"(?:[^。；，,？?！!]{0,6})$"
)


def _hint_negated(detail: str, start: int) -> bool:
    """Only the hint's own clause counts; a later contrast can correct it."""
    prefix = detail[:start]
    boundaries = list(_HINT_LOCAL_BOUNDARY.finditer(prefix))
    if boundaries:
        prefix = prefix[boundaries[-1].end() :]
    return bool(_HINT_LOCAL_NEGATION.search(prefix))


def _detail_self_inconsistent(detail: str) -> bool:
    if not detail:
        return False
    return any(
        not _hint_negated(detail, match.start())
        for match in _CONSISTENCY_HINT.finditer(detail)
    )


def _parse_recheck(
    content: str, suspect_ids: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Exact, unique suspect IDs only; malformed/duplicate entries are unusable."""
    payload = _parse_payload(content)
    if payload is None:
        return {}, {}
    entries: dict[str, dict] = {}
    for raw in payload.get("labels") or []:
        if not isinstance(raw, dict):
            return {}, {}
        claim_id = raw.get("claim_id")
        # Extra/unknown IDs make the whole repair output unusable: promoting
        # known IDs from the same malformed scope is unsafe.
        if not isinstance(claim_id, str) or claim_id not in suspect_ids:
            return {}, {}
        if claim_id in entries:
            return {}, {}
        entries[claim_id] = raw
    if set(entries) != suspect_ids:
        return {}, {}
    verdicts: dict[str, str] = {}
    details: dict[str, str] = {}
    for claim_id, raw in entries.items():
        label = raw.get("label")
        if isinstance(label, str):
            verdicts[claim_id] = label
            details[claim_id] = str(raw.get("detail") or "")
    return verdicts, details


def _item_payload(claim: Claim, evidence: list[Evidence] | None = None) -> dict:
    by_key = {item.key: item.article for item in evidence or []}
    sources = []
    for citation in claim.citations:
        article = by_key.get((citation.article_id, citation.part_no))
        header = table_header_context(
            evidence or [], citation.article_id, citation.part_no, citation.quote_start
        )
        source = {
            "article_id": citation.article_id,
            "part_no": citation.part_no,
            "article_title": citation.doc_title,
            "article_no": citation.article_no,
            "version_id": article.version_id if article else "",
            "quote": citation.quote,
            "table_role": table_role(
                header["columns"] if header else (),
                str(header["quote"]) if header else "",
                citation.article_no + citation.doc_title,
            ),
            "trusted_source_context": trusted_context(article) if article else {},
        }
        if header is not None:
            # Separate original header fragment; citation quote/offsets stay untouched.
            source["table_header"] = header
        sources.append(source)
    return {"claim_id": claim.claim_id, "claim": claim.text, "sources": sources}


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
        raw_details: dict[str, str] = {}
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
            full_detail = str(raw.get("detail") or "")
            raw_details[claim_id] = full_detail
            details[claim_id] = full_detail[:300]
        missing = expected - set(labels)
        for claim_id in missing:
            labels[claim_id] = SemanticLabel.INSUFFICIENT
            details[claim_id] = "语义模型未返回该条标签"
        checked = len(labels)
        suspects = [
            claim_id
            for claim_id, label in labels.items()
            if label is not SemanticLabel.SUPPORTED
            and _detail_self_inconsistent(raw_details.get(claim_id, ""))
        ]
        if suspects:
            suspect_ids = set(suspects)
            recheck_claims = [
                claim
                for claim in claims[: self._batch_size]
                if claim.claim_id in suspect_ids
            ]
            recheck_items = [_item_payload(claim, evidence) for claim in recheck_claims]
            checked += len(recheck_claims)
            recheck_started = time.perf_counter()
            try:
                recheck_response = self._provider.complete(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": RECHECK_PROMPT
                            + "\n"
                            + json.dumps(
                                {"items": recheck_items},
                                ensure_ascii=False,
                                indent=2,
                            ),
                        },
                    ],
                    temperature=0.0,
                    max_tokens=1200,
                )
            except ChatProviderError:
                latency += int((time.perf_counter() - recheck_started) * 1000)
                for claim_id in suspects:
                    details[claim_id] = (
                        "定向复核失败，保留原标签。 " + details[claim_id]
                    )[:300]
            else:
                latency += int((time.perf_counter() - recheck_started) * 1000)
                tokens_in += recheck_response.prompt_tokens
                tokens_out += recheck_response.completion_tokens
                model = recheck_response.model or model
                verdicts, recheck_details = _parse_recheck(
                    recheck_response.content, suspect_ids
                )
                for claim_id in suspects:
                    verdict = verdicts.get(claim_id)
                    if verdict == SemanticLabel.SUPPORTED.value:
                        labels[claim_id] = SemanticLabel.SUPPORTED
                        note = "定向复核：supported。"
                        if recheck_details.get(claim_id):
                            note += recheck_details[claim_id] + " "
                        details[claim_id] = (note + details[claim_id])[:300]
                    elif verdict:
                        details[claim_id] = (
                            f"定向复核：{verdict}，保留原标签。 "
                            + details[claim_id]
                        )[:300]
                    else:
                        details[claim_id] = (
                            "定向复核输出无效或缺失，保留原标签。 "
                            + details[claim_id]
                        )[:300]
        return SemanticReport(
            labels=labels,
            details=details,
            model=model,
            latency_ms=latency,
            checked=checked,
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

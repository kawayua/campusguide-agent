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
from campusguide.domain.enums import Audience, BlockType, ClaimKind
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
    provider_retried: bool = False
    json_empty_retried: bool = False
    json_truncation_retried: bool = False
    empty_claims_retried: bool = False
    empty_claims_error: str = ""


PROMPT_VERSION = "w6a2.4-2026-10-24"
# W6 图角色使用独立 prompt 版本；W6-A2 的契约常量保留给历史 run。
W6B_ADVISOR_PROMPT_VERSION = "w6b-advisor-2026-10-25"
REVISE_PROMPT_VERSION = "w6b-revise-2026-10-25"

FLOW_BOUNDARY_PROMPT = (
    "流程事实（步骤、材料、部门、截止时间）由系统 checklist 渲染；"
    "你只解释政策依据，不补步骤、不补材料、不写截止日期或部门。"
)

_ENUMERATION_RE = re.compile(r"哪些(?:情形|情况|条件|材料|要求|行为|档次|标准|项目)")
_MULTI_ASPECT_RE = re.compile(
    r"(?:分别|各自)|(?:方面.{0,20}(?:和|与|及))|(?:(?:和|与|及).{0,20}方面)"
)
_COURSE_CREDIT_INTENT_RE = re.compile(r"课程|学分|学时")

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


def _generation_evidence(question: str, evidence: list[Evidence]) -> list[Evidence]:
    """Keep neighboring sections from displacing a plan's multi-aspect goals answer."""
    if (
        "培养方案" not in question
        or not _MULTI_ASPECT_RE.search(question)
        or _COURSE_CREDIT_INTENT_RE.search(question)
    ):
        return evidence
    goal_sections = [
        item
        for item in evidence
        if item.article.block_type is BlockType.SECTION and "培养目标" in item.article.article_no
    ]
    return goal_sections or evidence


def _subquestions(question: str) -> list[str]:
    return [part.strip() for part in re.findall(r"[^？?]+[？?]", question)]


def _needs_larger_budget(question: str) -> bool:
    """多问或列举题输出更长，首轮就给更大预算，避免先截断再重试。"""
    return len(_subquestions(question)) > 1 or bool(_ENUMERATION_RE.search(question))


_JOB_INTENT_RE = re.compile(r"就业|职业|去向|毕业后|从事.{0,12}工作|工作方面")
_JOB_TEXT_RE = re.compile(
    r"(?:可在|可到|能够|能在)[^。；\n]{0,40}(?:从事|担任)[^。；\n]{0,40}(?:工作|岗位)"
)


def build_messages(
    question: str,
    evidence: list[Evidence],
    audience: Audience | None,
    *,
    flow_context: str = "",
) -> list[dict[str, str]]:
    generation_evidence = _generation_evidence(question, evidence)
    parts = [
        f"受众：{_audience_label(audience)}",
        f"问题：{question}",
    ]
    if flow_context:
        parts.extend(["", f"流程边界：{flow_context}"])
    subquestions = _subquestions(question)
    if len(subquestions) > 1:
        parts.extend(
            [
                "",
                f"本题包含 {len(subquestions)} 个子问题，必须逐问回答；"
                "每个子问题至少要有 1 条 claim，不能只回答其中一问；"
                "每个子问题优先选择直接回答该问题的证据，"
                "不要把申请资格、办理程序或相邻条款当成实体条件：",
            ]
        )
        parts.extend(f"子问题{i}：{item}" for i, item in enumerate(subquestions, start=1))
    parts.extend(
        [
            "",
            f"证据片段：\n{build_evidence_block(generation_evidence)}",
            "",
            "请先为每条 claim 选择完整的 citation.quote，再写 claim；"
            "quote 必须是同一证据正文中的连续子串；不得用省略号、不得跳过中间分项，"
            "也不得把“引导句 + 非相邻分项”拼接起来。"
            "claim 的时间/范围条件和数字/金额/期限必须原样出现在 quote 中。"
            "一条 claim 只表达一个条件、一个后果或一个数量；"
            "claim 不得超出 quote。quote 不完整时请缩短 claim、删除该 claim 或返回空 claims。",
        ]
    )
    if _ENUMERATION_RE.search(question):
        parts.append(
            "问题要求“哪些”具体情形/条件/材料/要求时，必须逐项覆盖证据中的"
            "（一）（二）……各分项；每个分项单独成 claim，不得只复述引导句，"
            "也不得只写“有下列情形之一”。"
        )
    if _MULTI_ASPECT_RE.search(question):
        parts.append(
            "问题明确询问多个方面或对象时，先识别每个方面/对象并逐项作答，"
            "每项至少一条 claim；只回答问题点名的内容，不要用相邻章节的其他要求替代，"
            "也不要补充问题未询问的条件。"
        )
    if generation_evidence is not evidence:
        parts.append(
            "本题询问培养方案的多个方面；证据已聚焦到该方案的培养目标 section，"
            "请按问题中的对象和方面逐项回答，不要引入其他章节的科研成果门槛或基本素质要求。"
        )
    if _JOB_INTENT_RE.search(question) and any(
        item.article.block_type is BlockType.SECTION
        and _JOB_TEXT_RE.search(item.article.text or "")
        for item in evidence
    ):
        parts.append(
            "如果某个 section 同时覆盖问题中的多个方面（例如研究/培养要求与"
            "毕业后可从事的工作），优先整体引用该 section；不要只用只覆盖"
            "单方面的相邻 section 替代。"
        )
    parts.append("请只输出符合 schema 的 claims JSON。")
    user = "\n".join(parts)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


REVISE_SYSTEM_PROMPT = """你是制度问答的 claim 修复器。只修复被标记失败的 claim。
必须遵守：
1. 只输出 JSON：{"claims":[...]}，不要解释、不要 Markdown。
2. 只能输出 failed_claim_ids 中的 claim_id；不得新增 claim，不得改写通过的 claim。
3. 每条 claim 仍必须满足：citation.article_id 逐字来自证据引用ID；
   citation.quote 是同一证据正文中的连续子串（至少 6 字），不得省略、不得拼接；
   claim 的数字/日期/范围/限定词必须原样出现在 quote 中。
4. 如果失败项无法用连续 quote 支持，直接省略该 claim，不要编造。
5. 流程事实由系统 checklist 渲染；不要补步骤、材料、部门或截止时间。"""


def build_revise_messages(
    question: str,
    evidence: list[Evidence],
    *,
    failed_claim_ids: list[str],
    issues: list[dict[str, Any]],
    previous_claims: list[ClaimDraft],
    audience: Audience | None,
    flow_context: str = "",
) -> list[dict[str, str]]:
    previous = [
        {
            "claim_id": draft.claim_id,
            "text": draft.text,
            "kind": draft.kind,
            "citations": [
                {"article_id": citation.article_id, "quote": citation.quote}
                for citation in draft.citations
            ],
        }
        for draft in previous_claims
    ]
    user = "\n".join(
        [
            f"受众：{_audience_label(audience)}",
            f"问题：{question}",
            f"failed_claim_ids：{json.dumps(failed_claim_ids, ensure_ascii=False)}",
            f"失败原因：{json.dumps(issues, ensure_ascii=False)}",
            f"上一轮 claims：{json.dumps(previous, ensure_ascii=False)}",
            ("流程边界：" + flow_context) if flow_context else "",
            "可用证据：",
            build_evidence_block(evidence),
            "",
            "请只返回失败 claim 的修订版；修不好就省略；不要新增 claim。",
        ]
    )
    return [
        {"role": "system", "content": REVISE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def merge_revised_drafts(
    previous: list[ClaimDraft],
    revised: list[ClaimDraft],
    failed_claim_ids: list[str],
) -> list[ClaimDraft]:
    """通过的 claim 原样保留；失败项有修订则替换，没有则保留原项。"""
    failed = set(failed_claim_ids)
    by_id = {draft.claim_id: draft for draft in revised if draft.claim_id in failed}
    merged: list[ClaimDraft] = []
    for draft in previous:
        if draft.claim_id in failed and draft.claim_id in by_id:
            merged.append(by_id[draft.claim_id])
        else:
            merged.append(draft)
    return merged


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


def _looks_truncated(content: str) -> bool:
    text = content.strip()
    return bool(text) and text.startswith("{") and not text.endswith("}")


def _retry_tokens(max_tokens: int) -> int:
    return min(6000, max(max_tokens * 2, 2000))


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
        retry_empty_claims: bool = True,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._enable_quote_repair = enable_quote_repair
        self._retry_empty_claims = retry_empty_claims

    def generate(
        self,
        question: str,
        evidence: list[Evidence],
        audience: Audience | None = None,
        *,
        flow_context: str = "",
    ) -> AdvisorResult:
        messages = build_messages(question, evidence, audience, flow_context=flow_context)
        responses: list[ChatResponse] = []
        provider_retried = False
        first_tokens = (
            _retry_tokens(self._max_tokens) if _needs_larger_budget(question) else self._max_tokens
        )
        try:
            first = self._provider.complete(
                messages,
                temperature=self._temperature,
                max_tokens=first_tokens,
            )
        except ChatProviderError:
            # 端点偶发超时/空响应：整题从头重试一次，仍失败才判 GENERATION_FAILED。
            provider_retried = True
            first = self._provider.complete(
                messages,
                temperature=0.0,
                max_tokens=first_tokens,
            )
        responses.append(first)
        json_repaired = False
        json_empty_retried = False
        json_truncation_retried = False
        try:
            drafts = parse_claims_json(first.content).claims
            draft_content = first.content
        except AdvisorError as first_error:
            # 首次输出为空或被截断时，直接提高 token 预算重试，避免在
            # 同一预算下反复截断。
            first_empty = not first.content.strip()
            first_truncated = _looks_truncated(first.content)
            repair_tokens = (
                _retry_tokens(self._max_tokens)
                if first_empty or first_truncated
                else self._max_tokens
            )
            repair_instruction = (
                "上面的输出无法解析为约定 JSON："
                f"{first_error}。请只重新输出一个合法 JSON 对象，"
                '结构为 {"claims": [...]}，不要代码块，不要解释。'
            )
            if first_empty or first_truncated:
                repair_instruction += (
                    "上次输出为空或被截断：减少 claim 数量（最多 3 条）并保持每条简短。"
                )
            repair_messages: list[dict[str, Any]] = [
                *messages,
                {"role": "assistant", "content": first.content},
                {"role": "user", "content": repair_instruction},
            ]
            try:
                second = self._provider.complete(
                    repair_messages,
                    temperature=0.0,
                    max_tokens=repair_tokens,
                )
            except ChatProviderError:
                raise
            responses.append(second)
            try:
                drafts = parse_claims_json(second.content).claims
                draft_content = second.content
                json_repaired = True
                if first_empty or first_truncated:
                    json_empty_retried = first_empty
                    json_truncation_retried = first_truncated
            except AdvisorError as second_error:
                # 端点偶发返回空内容：只追加一次空返回重试，非空但非法 JSON 不重试。
                retry_reason = ""
                if not second.content.strip():
                    retry_reason = "empty"
                elif "Unterminated" in str(second_error) or _looks_truncated(second.content):
                    retry_reason = "truncated"
                if retry_reason:
                    retry_messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                "上一次输出为空或被截断。请重新从证据生成："
                                "只输出一个合法 JSON 对象，减少 claim 数量（最多 3 条）"
                                '并保持每条简短；仍然无法回答时输出 {"claims": []}。'
                            ),
                        },
                    ]
                    retry_tokens = _retry_tokens(self._max_tokens)
                    try:
                        third = self._provider.complete(
                            retry_messages,
                            temperature=0.0,
                            max_tokens=retry_tokens,
                        )
                    except ChatProviderError:
                        raise
                    responses.append(third)
                    try:
                        drafts = parse_claims_json(third.content).claims
                    except AdvisorError as third_error:
                        label = "空返回重试" if retry_reason == "empty" else "截断重试"
                        raise AdvisorError(
                            f"JSON 修复失败（{label}）：{third_error}"
                        ) from third_error
                    draft_content = third.content
                    json_repaired = True
                    json_empty_retried = retry_reason == "empty"
                    json_truncation_retried = retry_reason == "truncated"
                else:
                    raise AdvisorError(f"JSON 修复失败：{second_error}") from second_error

        empty_claims_retried = False
        empty_claims_error = ""
        if not drafts and self._retry_empty_claims:
            empty_claims_retried = True
            empty_messages: list[dict[str, str]] = [
                *messages,
                {"role": "assistant", "content": draft_content},
                {
                    "role": "user",
                    "content": (
                        "上一次 claims 数组为空。请重新检查证据：如果存在可以直接引用、"
                        "且 citation.quote 能连续覆盖的结论，请输出 1-5 条 claim；"
                        '如果证据确实不足以回答，保持 {"claims": []}。不要编造。'
                    ),
                },
            ]
            try:
                empty_response = self._provider.complete(
                    empty_messages,
                    temperature=0.0,
                    max_tokens=first_tokens,
                )
                responses.append(empty_response)
                retried_drafts = parse_claims_json(empty_response.content).claims
                if retried_drafts:
                    drafts = retried_drafts
                    draft_content = empty_response.content
            except (ChatProviderError, AdvisorError) as exc:
                # 空 claims 重试失败不改变原拒答；交给 Verifier 返回 NO_VALID_CLAIM。
                empty_claims_error = f"{type(exc).__name__}: {exc}"

        result = self._repair_quotes(
            messages,
            draft_content,
            drafts,
            evidence,
            responses,
            json_repaired=json_repaired,
        )
        result.json_empty_retried = json_empty_retried
        result.json_truncation_retried = json_truncation_retried
        result.empty_claims_retried = empty_claims_retried
        result.empty_claims_error = empty_claims_error
        result.provider_retried = provider_retried
        return result

    def revise(
        self,
        question: str,
        evidence: list[Evidence],
        *,
        failed_claim_ids: list[str],
        issues: list[dict[str, Any]],
        previous_claims: list[ClaimDraft],
        audience: Audience | None = None,
        flow_context: str = "",
    ) -> AdvisorResult:
        """只修失败 claim；不新增、不改通过项、不引用证据外 ID。"""
        if not failed_claim_ids:
            return AdvisorResult(drafts=[])
        messages = build_revise_messages(
            question,
            evidence,
            failed_claim_ids=failed_claim_ids,
            issues=issues,
            previous_claims=previous_claims,
            audience=audience,
            flow_context=flow_context,
        )
        responses: list[ChatResponse] = []
        try:
            response = self._provider.complete(
                messages,
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except ChatProviderError:
            raise
        responses.append(response)
        repaired = False
        try:
            drafts = parse_claims_json(response.content).claims
        except AdvisorError:
            repair_messages: list[dict[str, Any]] = [
                *messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        "上面的输出不是合法 claims JSON。请只输出失败 claim 的修订版："
                        '{"claims":[...]}，不要解释。'
                    ),
                },
            ]
            second = self._provider.complete(
                repair_messages, temperature=0.0, max_tokens=self._max_tokens
            )
            responses.append(second)
            drafts = parse_claims_json(second.content).claims
            repaired = True
        failed = set(failed_claim_ids)
        filtered = [draft for draft in drafts if draft.claim_id in failed]
        result = AdvisorResult(drafts=filtered, responses=responses, repaired=repaired)
        problems = find_quote_problems(filtered, evidence)
        result.quote_repair_attempted = bool(problems)
        result.quote_repair_remaining = sorted({problem.claim_id for problem in problems})
        return result

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

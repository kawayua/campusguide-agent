"""Advisor：claim-first 生成。

模型只输出 claims JSON；正文由 Finalize 渲染，避免出现未被引用覆盖的句子。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatProvider, ChatProviderError, ChatResponse
from campusguide.agents.quote_repair import (
    SpanRegistry,
    build_quote_id_catalog,
    build_repair_messages,
    deterministic_quote_repair,
    find_quote_problems,
    is_scoped_span_id,
    matching_span_id,
    merge_repairs,
    remaining_problem_ids,
)
from campusguide.domain.enums import Audience, BlockType, ClaimKind
from campusguide.domain.models import Evidence
from campusguide.domain.source_context import trusted_context
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.source_spans import SourceSpan, source_spans
from campusguide.verification.coverage_items import required_course_items


class AdvisorError(RuntimeError):
    """模型输出不可解析，或重试后仍失败。"""


class CitationDraft(BaseModel):
    article_id: str = ""
    quote: str = ""
    quote_id: str = ""
    # 登记窗口的服务器端身份；只能由 quote_id 解析写入，模型文本不得提供。
    trusted_part_no: int | None = None
    trusted_start: int | None = None
    trusted_end: int | None = None


class ClaimDraft(BaseModel):
    claim_id: str | None = None
    text: str = ""
    kind: str = "other"
    unit_ids: list[str] = Field(default_factory=list)
    citations: list[CitationDraft] = Field(default_factory=list)


class ClaimsDraft(BaseModel):
    claims: list[ClaimDraft] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)


@dataclass
class AdvisorResult:
    drafts: list[ClaimDraft]
    responses: list[ChatResponse] = field(default_factory=list)
    repaired: bool = False
    quote_repair_attempted: bool = False
    quote_repaired: bool = False
    quote_repair_remaining: list[str] = field(default_factory=list)
    quote_repair_error: str = ""
    revision_error: str = ""
    provider_retried: bool = False
    unanswered_questions: list[str] = field(default_factory=list)
    json_empty_retried: bool = False
    json_truncation_retried: bool = False
    empty_claims_retried: bool = False
    empty_claims_error: str = ""


PROMPT_VERSION = "w6a2.5-2026-10-31"
# 主生成 prompt 身份被 W6-A2 旧契约测试冻结；初始 JSON 契约恢复单独版本化。
INITIAL_CONTRACT_RECOVERY_PROMPT_VERSION = "w6a2.6-2026-11-02-id-only-recovery"
# W6 图角色使用独立 prompt 版本；W6-A2 的契约常量保留给历史 run。
W6B_ADVISOR_PROMPT_VERSION = "w6b-advisor-quality-2026-10-31-table-structure"
REVISE_PROMPT_VERSION = "w6b-revise-quote-ids-2026-11-01"

FLOW_BOUNDARY_PROMPT = (
    "流程事实（步骤、材料、部门、截止时间）由系统 checklist 渲染；"
    "你只解释政策依据，不补步骤、不补材料、不写截止日期或部门。"
)

_ENUMERATION_RE = re.compile(r"哪些(?:情形|情况|条件|材料|要求|行为|档次|标准|项目)")
_MULTI_ASPECT_RE = re.compile(
    r"(?:分别|各自)|(?:方面.{0,20}(?:和|与|及))|(?:(?:和|与|及).{0,20}方面)"
)
_COURSE_CREDIT_INTENT_RE = re.compile(r"课程|学分|学时")

SYSTEM_PROMPT = """你是华南农业大学公开知识问答助手。只依据本轮证据，输出一个 JSON 对象：
{"claims":[{"claim_id":"c1","text":"直接回答问题的事实或完整列表","kind":"other",
"unit_ids":["u1"],"citations":[{"quote_id":"<本轮证据窗口ID>"}]}],"unanswered_questions":[]}
规则：
1. 完整覆盖用户所问的各对象、版本和方面。通常1—12条claim；同一对象/方面的完整列表可合为一条，
   同一条件下的多个具体要求可合为一条。不要只复述“应符合（一）至（五）”，应说明具体条件。
   不同奖项、导师制度、专业或年份分别说明，不得混用。所有事实均须引用。
   只回答用户要求的方面；只问课程名称时不要额外添加课程代码、每门学分等数字。
   表格数字必须结合原始表头解释并同时引用表头和对应行；没有表头时不要猜列含义。
   表头与课程行可能分属不同原始片段：分别引用表头和行（可各用一个窗口ID），不得把两段拼成一个quote。
   结构化表格行给出“列路径与行值”时，只能按该列路径读取学分、总学时、理论/实验/实践学时、周数、学期；
   不得把同一行的其他列数字或另一行数字套到该事实。未提供可靠结构（旧版扁平/多层表头、校验未通过）时
   只引用原文，不推断上述字段。适用性矩阵行只说明支撑关系，不是课程清单或学分来源。
   用户未询问辅修或辅修学位时，不要把表格备注中的辅修学分要求当作普通主修学生的模块要求。
   课程名单优先选择“课程代码+课程名称”的进程表原始行，不把“培养目标实现矩阵”的实现途径当课程要求。
   实践课程包括实验教学和其他实践；只问集中实践时再按集中环节筛选，不能把实践课程默认为毕业实习。
2. 优先使用证据提供的原文窗口ID作为citation.quote_id；只能从本轮证据块列出的窗口ID中原样选择，
   示例仅表示结构，不是固定ID。系统会逐字取回该窗口正文和article_id，无需手工复制。
   窗口模式只输出quote_id，不再同时填写quote或article_id，避免冲突。
   也可沿用quote字符串模式。article_id逐字取自证据。quote必须逐字复制对应正文中一个至少6字的连续子串。
   不得省略、改写、跳过中间分项或拼接不相邻句子。需要多处分散证据时用多个citation，各自独立连续。
   完整列表可引用包含引导句和所有分项的长quote，不要为了简短而删去必需答案。
3. 逐条自检：条件 + 后果、限定词和证据必须匹配。不得补充证据以外的事实。不要展开无依据的内容。
   主体归属、名单所属系、适用专业、表头含义可结合同一文档可信标题/章节解释。
   姓名排版空格可按名单边界识别（例如姓名两字之间的单个空格），不得拼接两个相邻完整姓名。
   此上下文不能补出正文没有的金额、期限、课程、角色关系、数字或明确的成员身份。
4. 包括“一学期内”等时间范围必须有原文支持。一个条件、一个后果或一个数量都要完整核对；
   允许同一方面的多个列表项合并，但不能混合不相容的条件。数字、金额、期限、日期、课程、第一/第二导师等实质关系必须由所引正文支持。
   不得把标题的采集日期当新闻发布日期；日期列表行的开头才是该条消息发布日期。
   “最新”仅限已提供的完整收录集合；不得把开会日期与发布日期互换。
5. 保留原文“一般、原则上、可以、应当、不得、至少、最多”等限定。不能从科研平台数推断实验室总量，
   不能从局部名单推断全院人数。不能套用别的专业或届别；引用标题应让读者知道适用对象。
6. 列表/比较/多问题必须列全证据支持的核心项，并对缺少证据的方面明确标记缺口。
   unanswered_questions只列本轮所有claim及系统流程清单均未回答的必要方面，全部答完才为空。
   已由系统checklist回答的材料/时间/步骤不要再说漏答。
7. 如果无法回答，claims可为空，但必须具体列出缺证据的方面。不猜测未收录事实。
8. unit_ids只能使用系统提供的标识；未提供问题单元时可为空。
9. kind只能取eligibility / step / material / deadline / department / version / definition / other。
"""


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


def build_evidence_block(
    evidence: list[Evidence],
    *,
    spans: dict[str, SourceSpan] | None = None,
) -> str:
    blocks: list[str] = []
    spans = source_spans(evidence) if spans is None else spans
    evidence_index = {
        (item.article.article_id, item.article.part_no): index
        for index, item in enumerate(evidence, 1)
    }
    header_rows: dict[tuple[str, int], list[str]] = {}
    for span in spans.values():
        if span.header_part_no is None:
            continue
        header_key = (span.header_article_id, span.header_part_no)
        header_index = evidence_index.get(header_key)
        row_index = evidence_index.get((span.article_id, span.part_no))
        if header_index is None or row_index is None:
            continue
        header_rows.setdefault(header_key, []).append(f"证据{row_index} {span.span_id}")
    for index, item in enumerate(evidence, 1):
        article = item.article
        pointers = []
        for span in spans.values():
            if not span.span_id.startswith(f"source_{index}_"):
                continue
            pointer = f"{span.span_id}：{span.label}" + (
                f"；模块={span.module}" if span.module else ""
            )
            if is_scoped_span_id(span.span_id):
                pointer += (
                    f"；part_no={span.part_no}；原文偏移={span.start}-{span.end}"
                    f"；原文片段={json.dumps(span.quote, ensure_ascii=False)}"
                )
            if span.structured:
                pointer += f"；表格ID={span.table_id}；行ID={span.row_id}"
                if span.span_id.endswith("_body"):
                    pointer += "；完整正文含表头与行，列语义见本片段行窗口"
                elif span.curriculum_row:
                    details = "；".join(
                        f"{path}={value}" for path, value in span.column_values if value
                    )
                    pointer += (
                        "；结构化表格列路径与行值：" + (details or "（空）")
                        + "；只按列路径引用对应列数字，不能跨列/跨行套用"
                    )
                elif span.header_reliable:
                    pointer += "；适用性矩阵仅表示支撑关系，不是课程清单或学分来源"
                else:
                    problems = "、".join(span.structure_problems[:4])
                    pointer += "；结构化表格校验未通过"
                    if problems:
                        pointer += f"（{problems}）"
                    pointer += "，只引用原文，不推断学分/学时/学期"
                if not span.header_reliable and span.span_id.endswith("_body"):
                    pointer += "；列语义不可靠，只引用原文，不推断学分/学时/学期"
            elif span.header_part_no is not None:
                header_index = evidence_index.get((span.header_article_id, span.header_part_no))
                if header_index is not None:
                    # Table header and row stay separate original quotes; this
                    # pointer only tells the model which header evidence to cite.
                    pointer += f"；同表表头=证据{header_index}"
            if not span.structured and not span.header_reliable:
                pointer += "；列语义不可解析（扁平/多层表头），只引用原文，不推断学分/学时/学期"
            pointers.append(pointer)
        location = article.article_no
        confidence = "（日期为锚点，非精确发布日）" if article.date_confidence == "low" else ""
        lines = [
            f"[证据{item.rank}]",
            f"引用ID：{article.article_id}",
            f"可信来源标题：{article.doc_title}",
            "可信来源元数据：" + json.dumps(trusted_context(article), ensure_ascii=False),
            f"可信章节：{' / '.join(article.chapter_path)}",
            f"原始片段part_no：{article.part_no}",
            f"位置：{location}（{article.block_type.value}）",
            f"生效日期：{article.effective_from.isoformat()}{confidence}",
            "原文窗口ID（可用citation.quote_id引用）：" + "；".join(pointers),
        ]
        related = header_rows.get((article.article_id, article.part_no))
        if related:
            lines.append(
                "同表后续行（引用行时须同时引用本表头）：" + "；".join(related)
            )
        lines.extend(["正文：", article.text])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _generation_evidence(question: str, evidence: list[Evidence]) -> list[Evidence]:
    if (
        "培养方案" in question
        and _MULTI_ASPECT_RE.search(question)
        and not re.search(r"课程|学分|学时|科目|选修|实践|毕业设计", question)
    ):
        goals = [item for item in evidence if "培养目标" in item.article.article_no]
        # Restrict only a research/employment interpretation of the goals section.
        if goals and _JOB_INTENT_RE.search(question):
            return goals
    return evidence


def _subquestions(question: str) -> list[str]:
    parts: list[str] = []
    for sentence in re.split(r"[？?；;]+", question):
        sentence = sentence.strip()
        if not sentence:
            continue
        clauses = [part.strip() for part in re.split(r"[，,]+", sentence) if part.strip()]
        interrogative = re.compile(r"吗|谁|哪|多少|什么|如何|怎样|怎么|何时|是否|能否|有何")
        if len(clauses) > 1 and sum(bool(interrogative.search(c)) for c in clauses) > 1:
            parts.extend(clauses)
        else:
            parts.append(sentence)
    return parts


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
    question_units: list[QuestionUnit] | None = None,
    class_terms: dict[str, tuple[str, ...]] | None = None,
    evidence_scope: list[Evidence] | None = None,
) -> list[dict[str, str]]:
    generation_evidence = (
        _generation_evidence(question, evidence)
        if evidence_scope is None
        else evidence_scope
    )
    parts = [
        f"受众：{_audience_label(audience)}",
        f"问题：{question}",
    ]
    if question_units:
        parts.extend(
            [
                "",
                "规范化问题单元（须全部检查，多个单元可共享一条claim）：",
                json.dumps(
                    [
                        {
                            **u.model_dump(mode="json"),
                            "required_source_items": required_course_items(
                                u, evidence, class_terms=class_terms
                            ),
                        }
                        for u in question_units
                    ],
                    ensure_ascii=False,
                ),
            ]
        )
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
            "按对象与方面直接作答。完整列出原文支持的课程、条件或项目，不只写条号。"
            "每个quote都须保持原文连续，分散的事实分别引用；保留范围和限定词。",
        ]
    )
    if _ENUMERATION_RE.search(question):
        parts.append("不得只复述引导句，应列出原文支持的必要分项。")
    if _MULTI_ASPECT_RE.search(question):
        parts.append("先识别每个方面/对象并逐项作答，不要用相邻章节的其他要求替代。")
    if generation_evidence != evidence:
        parts.append("不要引入其他章节的科研成果门槛，按对应培养目标解释研究与就业。")
    parts.append(
        "claim 不得超出 quote 与可信来源元数据的允许范围；正文引用必须为连续子串，不可拼接非相邻分项。"
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
2. 只能输出 failed_claim_ids 中的 claim_id；不得新增 claim，不得重复 claim_id，不得改写通过的 claim。
3. 每个 citation 必须且只能是一个对象 {"quote_id":"..."}；quote_id 只能取本轮证据窗口ID
   或失败项 citations 给出的 ID。不得输出 article_id，不得输出 quote 或任何重抄正文。
   系统会按ID逐字取回原文连续子串和 article_id，模型文本永远不作为事实来源。
4. 优先保留上一轮已由 quote 充分支持、且用户问题明确要求的事实与限定；只针对失败原因
   修改具体事实、数字、范围或必要限定，不要为了补全摘要而增加问题未要求、原文未写出的无关事实。
5. 数字、日期、范围、限定词必须原样出现在所引窗口原文中；不得省略、不得拼接，
   不得把首尾或不同窗口拼成一条引用。
6. 不同动作各有期限、条件或主体时，不要合并为“同期、绑定、因果或同一人”关系，除非所引原文
   明确如此；原文中使 claim 成立所必需的关键限制或例外，仍必须保留。
7. 如果失败项无法用现有窗口ID支持，直接省略该 claim，不要编造。
8. 流程事实由系统 checklist 渲染；不要补步骤、材料、部门或截止时间。"""


def _requirements_block(requirements: list[dict[str, Any]] | None) -> str:
    """方案 A2：把“必须逐项覆盖”的清单写成改写提示的一部分。"""
    if not requirements:
        return ""
    lines = ["必须覆盖（只能使用原文出现的名称，不得改写、合并或补充）："]
    for item in requirements:
        type_word = item.get("type_word") or ""
        names = item.get("required_items") or []
        if not names:
            continue
        lines.append(f"- 类型：{type_word}")
        lines.append("- 必须逐项列出：" + "；".join(names))
        forbidden = item.get("forbidden_types") or []
        if forbidden:
            lines.append("- 不得把以下类型计入：" + "、".join(forbidden))
    lines.append("未覆盖上述任一项时，不要输出该结论，留给缺口说明。")
    return "\n".join(lines)


@dataclass
class RevisionPrompt:
    messages: list[dict[str, str]]
    registry: SpanRegistry


def _revision_previous_payload(
    previous_claims: list[ClaimDraft],
    failed_claim_ids: set[str],
    registry: SpanRegistry,
) -> list[dict[str, Any]]:
    """Failed claims only, with registered IDs instead of retyped quotes."""
    payload: list[dict[str, Any]] = []
    for draft in previous_claims:
        if draft.claim_id not in failed_claim_ids:
            continue
        citations: list[dict[str, Any]] = []
        for citation in draft.citations:
            ids: list[str] = []
            quote_id = matching_span_id(citation, registry.spans)
            if quote_id:
                ids.append(quote_id)
            for span_id in registry.ids_for_quote(
                citation.article_id, citation.quote, register=True
            ):
                if span_id not in ids:
                    ids.append(span_id)
            if ids:
                citations.append({"quote_ids": ids})
            else:
                citations.append(
                    {
                        "article_id": citation.article_id,
                        "status": "旧引用无法映射到当前证据，请改用可用窗口ID",
                    }
                )
        payload.append(
            {
                "claim_id": draft.claim_id,
                "text": draft.text,
                "kind": draft.kind,
                "citations": citations,
            }
        )
    return payload


def build_revise_messages(
    question: str,
    evidence: list[Evidence],
    *,
    failed_claim_ids: list[str],
    issues: list[dict[str, Any]],
    previous_claims: list[ClaimDraft],
    audience: Audience | None,
    flow_context: str = "",
    requirements: list[dict[str, Any]] | None = None,
    registry: SpanRegistry | None = None,
    contract_error: str = "",
) -> list[dict[str, str]]:
    registry = registry or SpanRegistry.build(evidence)
    previous = _revision_previous_payload(
        previous_claims, set(failed_claim_ids), registry
    )
    parts = [
        f"受众：{_audience_label(audience)}",
        f"问题：{question}",
        f"failed_claim_ids：{json.dumps(failed_claim_ids, ensure_ascii=False)}",
        f"失败原因：{json.dumps(issues, ensure_ascii=False)}",
        "上一轮失败 claims（citations 只给可用 quote_id，不再提供旧 quote）："
        + json.dumps(previous, ensure_ascii=False),
    ]
    if contract_error:
        parts.append(f"上一次修订未通过严格引用契约：{contract_error}")
    if flow_context:
        parts.append("流程边界：" + flow_context)
    requirements_block = _requirements_block(requirements)
    if requirements_block:
        parts.append(requirements_block)
    parts.extend(
        [
            "可用证据（citation.quote_id 只能取下列窗口ID）：",
            build_evidence_block(evidence, spans=registry.spans),
            "",
            '请只返回失败 claim 的修订版；每个 citation 只能是 {"quote_id":"..."}；'
            "修不好就省略；不要新增 claim，不要输出通过项。",
        ]
    )
    return [
        {"role": "system", "content": REVISE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_revision_prompt(
    question: str,
    evidence: list[Evidence],
    *,
    failed_claim_ids: list[str],
    issues: list[dict[str, Any]],
    previous_claims: list[ClaimDraft],
    audience: Audience | None,
    flow_context: str = "",
    requirements: list[dict[str, Any]] | None = None,
    contract_error: str = "",
) -> RevisionPrompt:
    registry = SpanRegistry.build(evidence)
    messages = build_revise_messages(
        question,
        evidence,
        failed_claim_ids=failed_claim_ids,
        issues=issues,
        previous_claims=previous_claims,
        audience=audience,
        flow_context=flow_context,
        requirements=requirements,
        registry=registry,
        contract_error=contract_error,
    )
    return RevisionPrompt(messages=messages, registry=registry)


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


_CITATION_CONTRACT_ERROR_MARKERS = (
    "引用窗口ID",
    "引用窗口与article_id不一致",
    "quote与登记窗口不一致",
)


def _is_citation_contract_error(error: AdvisorError) -> bool:
    message = str(error)
    return any(marker in message for marker in _CITATION_CONTRACT_ERROR_MARKERS)


def _id_only_contract_lines() -> list[str]:
    return [
        '每个 citation 必须且只能是 {"quote_id":"<本轮证据中已存在的窗口ID>"}；',
        "不得再输出 article_id、quote 或复制窗口正文等其他字段；",
        "本轮证据窗口清单保持不变（与首次生成相同），未知ID不会被别名映射或回退。",
    ]


def _resolve_citation(
    citation: Any,
    spans: dict[str, SourceSpan],
    *,
    strict_quote_ids: bool,
    seen_quote_ids: set[str],
) -> CitationDraft:
    if not isinstance(citation, dict):
        if strict_quote_ids:
            raise AdvisorError("严格修订契约要求 citation 是对象")
        return CitationDraft()
    quote_id = citation.get("quote_id")
    if strict_quote_ids:
        extra_keys = sorted(set(citation) - {"quote_id"})
        if extra_keys:
            raise AdvisorError(
                "严格修订契约只允许 citation.quote_id，"
                f"不得同时提供 {', '.join(extra_keys)}"
            )
        if not isinstance(quote_id, str) or not quote_id:
            raise AdvisorError("严格修订契约要求 citation.quote_id")
        span = spans.get(quote_id)
        if span is None:
            raise AdvisorError(f"引用窗口ID不在本轮证据中：{quote_id}")
        if quote_id in seen_quote_ids:
            raise AdvisorError(f"同一 claim 重复引用窗口ID：{quote_id}")
        seen_quote_ids.add(quote_id)
        return CitationDraft(
            article_id=span.article_id,
            quote=span.quote,
            quote_id=span.span_id,
            trusted_part_no=span.part_no,
            trusted_start=span.start,
            trusted_end=span.end,
        )
    if quote_id:
        if not isinstance(quote_id, str):
            raise AdvisorError("引用窗口ID必须是字符串")
        span = spans.get(quote_id)
        if span is None:
            raise AdvisorError(f"引用窗口ID不在本轮证据中：{quote_id}")
        if citation.get("article_id") and citation["article_id"] != span.article_id:
            raise AdvisorError("引用窗口与article_id不一致")
        if citation.get("quote") and citation["quote"] != span.quote:
            raise AdvisorError("quote与登记窗口不一致")
        return CitationDraft(
            article_id=span.article_id,
            quote=span.quote,
            quote_id=span.span_id,
            trusted_part_no=span.part_no,
            trusted_start=span.start,
            trusted_end=span.end,
        )
    return CitationDraft(
        article_id=(
            citation.get("article_id")
            if isinstance(citation.get("article_id"), str)
            else ""
        ),
        quote=(
            citation.get("quote") if isinstance(citation.get("quote"), str) else ""
        ),
    )


def parse_claims_json(
    content: str,
    evidence: list[Evidence] | None = None,
    *,
    strict_quote_ids: bool = False,
    allowed_claim_ids: set[str] | None = None,
    spans: dict[str, SourceSpan] | None = None,
) -> ClaimsDraft:
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
    registered_spans = source_spans(evidence or []) if spans is None else spans
    claims: list[ClaimDraft] = []
    seen_claim_ids: set[str] = set()
    for item in payload["claims"]:
        if not isinstance(item, dict):
            raise AdvisorError("claims 数组元素必须是对象")
        raw_claim_id = item.get("claim_id")
        claim_id = (
            raw_claim_id
            if isinstance(raw_claim_id, str) and raw_claim_id
            else None
        )
        if strict_quote_ids:
            if claim_id is None:
                raise AdvisorError("严格修订契约要求每个 claim 提供 claim_id")
            if allowed_claim_ids is not None and claim_id not in allowed_claim_ids:
                raise AdvisorError(f"修订输出包含未失败的 claim_id：{claim_id}")
            if claim_id in seen_claim_ids:
                raise AdvisorError(f"修订输出重复 claim_id：{claim_id}")
            seen_claim_ids.add(claim_id)
        citations = item.get("citations") or []
        if not isinstance(citations, list):
            raise AdvisorError("citations 必须是数组")
        if strict_quote_ids and not citations:
            raise AdvisorError(f"claim {claim_id} 缺少 citation.quote_id")
        seen_quote_ids: set[str] = set()
        claims.append(
            ClaimDraft(
                claim_id=claim_id,
                text=item.get("text") if isinstance(item.get("text"), str) else "",
                kind=item.get("kind") if isinstance(item.get("kind"), str) else "other",
                unit_ids=[v for v in item.get("unit_ids", []) if isinstance(v, str)]
                if isinstance(item.get("unit_ids", []), list)
                else [],
                citations=[
                    _resolve_citation(
                        citation,
                        registered_spans,
                        strict_quote_ids=strict_quote_ids,
                        seen_quote_ids=seen_quote_ids,
                    )
                    for citation in citations
                ],
            )
        )
    unanswered = payload.get("unanswered_questions", [])
    if not isinstance(unanswered, list) or any(not isinstance(item, str) for item in unanswered):
        raise AdvisorError('"unanswered_questions" 必须是字符串数组')
    return ClaimsDraft(claims=claims, unanswered_questions=unanswered)


def _parse_revision_claims(
    content: str,
    evidence: list[Evidence],
    registry: SpanRegistry,
    *,
    allowed_claim_ids: set[str],
) -> list[ClaimDraft]:
    """新模型输出只接受 quote_id；旧引用只能在生成 prompt 前由服务器登记。"""
    return parse_claims_json(
        content,
        evidence,
        strict_quote_ids=True,
        allowed_claim_ids=allowed_claim_ids,
        spans=registry.spans,
    ).claims


def _revision_retry_instruction(
    error: AdvisorError,
    *,
    failed_claim_ids: list[str],
    registry: SpanRegistry,
) -> str:
    span_ids = list(registry.spans)
    shown = span_ids[:40]
    suffix = "；……（其余见原证据块）" if len(span_ids) > len(shown) else ""
    return "\n".join(
        [
            f"上面的修订输出不符合严格引用契约：{error}",
            "修正后只输出失败 claim 的修订版，并严格遵守：",
            '- 顶层必须是 {"claims":[...]}；claim_id 只能取：'
            + json.dumps(failed_claim_ids, ensure_ascii=False),
            '- 每个 citation 必须且只能是 {"quote_id":"..."}；不得输出 article_id 或 quote；',
            "- 不得重复 claim_id；同一 claim 内不得重复 quote_id；",
            "- 可用 quote_id：" + json.dumps(shown, ensure_ascii=False) + suffix,
            '无法修复时输出 {"claims": []} 或省略该 claim，不要编造。',
        ]
    )


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
        profile_lexicon: ProfileLexicon | None = None,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._enable_quote_repair = enable_quote_repair
        self._retry_empty_claims = retry_empty_claims
        self._class_terms = profile_lexicon.class_terms if profile_lexicon else None

    def generate(
        self,
        question: str,
        evidence: list[Evidence],
        audience: Audience | None = None,
        *,
        flow_context: str = "",
        question_units: list[QuestionUnit] | None = None,
    ) -> AdvisorResult:
        generation_evidence = _generation_evidence(question, evidence)
        messages = build_messages(
            question,
            evidence,
            audience,
            flow_context=flow_context,
            question_units=question_units,
            class_terms=self._class_terms,
            evidence_scope=generation_evidence,
        )
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
        except ChatProviderError as exc:
            if not exc.retryable:
                raise
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
            drafts = parse_claims_json(first.content, generation_evidence).claims
            draft_content = first.content
        except AdvisorError as first_error:
            # 首次输出为空或被截断时，直接提高 token 预算重试，避免在
            # 同一预算下反复截断。
            window_catalog = build_quote_id_catalog(generation_evidence)
            first_empty = not first.content.strip()
            first_truncated = _looks_truncated(first.content)
            repair_tokens = (
                _retry_tokens(self._max_tokens)
                if first_empty or first_truncated
                else self._max_tokens
            )
            if _is_citation_contract_error(first_error):
                # 引用语义契约失败：不回显错误的 assistant JSON，避免模型
                # 继续复制 quote_id 与矛盾 quote，只用错误原因和原问题/证据重述规则。
                repair_instruction = "\n".join(
                    [
                        f"上一次输出违反了引用契约：{first_error}。",
                        "请重新从本轮证据生成：只输出一个合法 JSON 对象，"
                        '结构为 {"claims": [...]}，不要代码块，不要解释。',
                        *_id_only_contract_lines(),
                    ]
                )
                repair_messages = [
                    *messages,
                    {"role": "user", "content": repair_instruction},
                ]
            else:
                repair_instruction = (
                    "上面的输出无法解析为约定 JSON："
                    f"{first_error}。请只重新输出一个合法 JSON 对象，"
                    '结构为 {"claims": [...]}，不要代码块，不要解释。'
                )
                repair_instruction += "\n" + "\n".join(_id_only_contract_lines())
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": first.content},
                    {"role": "user", "content": repair_instruction},
                ]
            if window_catalog:
                repair_messages[-1]["content"] += (
                    "\n可用窗口ID（只能原样使用；article_id/part_no 为服务器身份）：\n"
                    + window_catalog
                )
            if first_empty or first_truncated:
                repair_messages[-1]["content"] += (
                    "上次输出为空或被截断：减少 claim 数量（最多 3 条）并保持每条简短。"
                )
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
                drafts = parse_claims_json(second.content, generation_evidence).claims
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
                    retry_instruction = "\n".join(
                        [
                            "上一次输出为空或被截断。请重新从证据生成："
                            "只输出一个合法 JSON 对象，减少 claim 数量（最多 3 条）"
                            '并保持每条简短；仍然无法回答时输出 {"claims": []}。',
                            *_id_only_contract_lines(),
                        ]
                    )
                    if window_catalog:
                        retry_instruction += (
                            "\n可用窗口ID（只能原样使用；article_id/part_no 为服务器身份）：\n"
                            + window_catalog
                        )
                    retry_messages = [
                        *messages,
                        {"role": "user", "content": retry_instruction},
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
                        drafts = parse_claims_json(third.content, generation_evidence).claims
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
                retried_drafts = parse_claims_json(
                    empty_response.content, generation_evidence
                ).claims
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
            generation_evidence,
            responses,
            json_repaired=json_repaired,
        )
        result.json_empty_retried = json_empty_retried
        result.json_truncation_retried = json_truncation_retried
        result.empty_claims_retried = empty_claims_retried
        result.empty_claims_error = empty_claims_error
        result.provider_retried = provider_retried
        result.unanswered_questions = parse_claims_json(
            draft_content, generation_evidence
        ).unanswered_questions[:12]
        if question_units:
            known_ids = {unit.unit_id for unit in question_units}
            for draft in result.drafts:
                if set(draft.unit_ids) - known_ids:
                    raise AdvisorError("模型返回未知问题单元标识")
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
        requirements: list[dict[str, Any]] | None = None,
    ) -> AdvisorResult:
        """只修失败 claim；引用只接受登记窗口 ID，不新增、不改通过项。"""
        if not failed_claim_ids:
            return AdvisorResult(drafts=[])
        failed = set(failed_claim_ids)
        prompt = build_revision_prompt(
            question,
            evidence,
            failed_claim_ids=failed_claim_ids,
            issues=issues,
            previous_claims=previous_claims,
            audience=audience,
            flow_context=flow_context,
            requirements=requirements,
        )
        responses: list[ChatResponse] = []
        response = self._provider.complete(
            prompt.messages,
            temperature=0.0,
            max_tokens=self._max_tokens,
        )
        responses.append(response)
        repaired = False
        try:
            drafts = _parse_revision_claims(
                response.content,
                evidence,
                prompt.registry,
                allowed_claim_ids=failed,
            )
        except AdvisorError as first_error:
            retry_messages: list[dict[str, Any]] = [
                *prompt.messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": _revision_retry_instruction(
                        first_error,
                        failed_claim_ids=failed_claim_ids,
                        registry=prompt.registry,
                    ),
                },
            ]
            second = self._provider.complete(
                retry_messages, temperature=0.0, max_tokens=self._max_tokens
            )
            responses.append(second)
            try:
                drafts = _parse_revision_claims(
                    second.content,
                    evidence,
                    prompt.registry,
                    allowed_claim_ids=failed,
                )
                repaired = True
            except AdvisorError as second_error:
                # 契约连续失败不应升级为整图 GENERATION_FAILED：返回空修订，
                # 由图层 merge 保留已通过 claim，Verifier 只丢弃失败项。
                return AdvisorResult(
                    drafts=[],
                    responses=responses,
                    repaired=False,
                    revision_error=(
                        f"严格修订契约失败：{first_error}；"
                        f"重试后仍失败：{second_error}"
                    ),
                )
        filtered = [draft for draft in drafts if draft.claim_id in failed]
        result = AdvisorResult(drafts=filtered, responses=responses, repaired=repaired)
        problems = find_quote_problems(filtered, evidence)
        result.quote_repair_attempted = bool(problems)
        if problems:
            deterministic = deterministic_quote_repair(filtered, problems, evidence)
            if deterministic.changed:
                filtered = deterministic.drafts
                result.drafts = filtered
                result.quote_repaired = True
                problems = find_quote_problems(filtered, evidence)
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
        registry = SpanRegistry.build(evidence)
        candidates_by_claim = registry.candidates_for_claims(drafts, problems)
        span_ids_by_claim = {
            claim_id: candidates.span_ids
            for claim_id, candidates in candidates_by_claim.items()
        }
        repair_messages = build_repair_messages(
            messages,
            draft_content,
            drafts,
            problems,
            spans=registry.spans,
            span_ids_by_claim=span_ids_by_claim,
            candidates_by_claim=candidates_by_claim,
        )
        merged = drafts
        merged_changed = False
        try:
            repaired_response = self._provider.complete(
                repair_messages,
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except ChatProviderError as exc:
            # 修复调用失败不阻塞整轮；保留原 draft，让 Verifier 按契约丢弃。
            result.quote_repair_error = f"{type(exc).__name__}: {exc}"
            result.quote_repair_remaining = [problem.claim_id for problem in problems]
            return result
        else:
            responses.append(repaired_response)
            try:
                repaired_drafts = _parse_revision_claims(
                    repaired_response.content,
                    evidence,
                    registry,
                    allowed_claim_ids={problem.claim_id for problem in problems},
                )
            except AdvisorError as exc:
                result.quote_repair_error = str(exc)
                result.quote_repair_remaining = [
                    problem.claim_id for problem in problems
                ]
                return result
            merged_result = merge_repairs(drafts, repaired_drafts, problems)
            merged = merged_result.drafts
            merged_changed = merged_result.changed

        # The established ID-only model attempt runs first. Only unresolved
        # table-fact gaps use the additive deterministic fallback, so a model
        # that selected the correct window is never expanded with extra quotes.
        remaining = find_quote_problems(merged, evidence)
        if remaining:
            deterministic = deterministic_quote_repair(
                merged, remaining, evidence, registry=registry
            )
            if deterministic.changed:
                merged = deterministic.drafts
                merged_changed = True
        result.drafts = merged
        result.quote_repaired = merged_changed
        result.quote_repair_remaining = remaining_problem_ids(merged, evidence)
        return result

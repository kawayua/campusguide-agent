"""W6 完整四角色图：Planner → Flow / Retriever → Advisor → Verifier → Finalize。

节点只通过 ``GraphState`` 交互；FlowExecutor 不调用 LLM，只执行 W5
``evaluate_flow``。revise 回到 Advisor（≤2），clarify 回到 Planner（≤3），
其余路径统一进 Finalize。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from campusguide.agents.advisor import (
    FLOW_BOUNDARY_PROMPT,
    Advisor,
    AdvisorError,
    CitationDraft,
    ClaimDraft,
    merge_revised_drafts,
)
from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import Plan, QueryPlanItem
from campusguide.agents.planner import Planner, PlannerContext
from campusguide.agents.providers import ChatProviderError
from campusguide.agents.retriever import GraphRetriever
from campusguide.agents.state import GraphState
from campusguide.agents.verifier import GraphVerifier
from campusguide.config import HandoffConfig
from campusguide.domain.enums import (
    Audience,
    BlockType,
    GraphDecision,
    Intent,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import ChecklistItem, Claim, Evidence, PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.domain.text import normalize_text
from campusguide.flows.catalog import FlowCatalog
from campusguide.handoff import resolve_handoff
from campusguide.observability.errors import safe_error_summary
from campusguide.observability.redact import redact_sensitive
from campusguide.observability.trace import TraceWriter
from campusguide.qa.finalize import (
    DISCLAIMER,
    finalize,
    finalize_abstain,
    finalize_flow,
    finalize_flow_without_claims,
    finalize_mixed,
)
from campusguide.qa.merge import merge_claims
from campusguide.qa.service import _MESSAGES as QA_MESSAGES
from campusguide.retrieval.budget import evidence_cost
from campusguide.verification.request_scope import (
    RequestedScope,
    filter_evidence_for_request,
    parse_requested_scope,
    requested_scope_message,
    source_applicability,
)

GRAPH_VERSION = "w6-quality-2026-10-28"

_DEFAULT_MESSAGES = {
    OutcomeCode.NO_EVIDENCE: "没有检索到可引用的现行条款。请补充具体事项或换一种说法。",
    OutcomeCode.UNVERIFIABLE: "检索到的材料未能通过语义/引用核对，因此不作答。",
    OutcomeCode.ONLY_REPEALED: "只检索到已废止版本，不对废止材料作答。",
    OutcomeCode.VERSION_MISMATCH: "检索到的材料不在当前有效期内，因此不作答。",
    OutcomeCode.VERSION_CONFLICT: "检索到多个并存版本，无法确定适用版本。",
    OutcomeCode.NO_VALID_CLAIM: "检索到了材料，但没有给出可核对的结论。",
    OutcomeCode.GENERATION_FAILED: "生成服务暂时不可用或输出不符合契约。",
    OutcomeCode.INDEX_UNAVAILABLE: "索引不可用或与 manifest 不一致。",
    OutcomeCode.TOOL_UNAVAILABLE: "检索工具不可用。",
    OutcomeCode.INVALID_REQUEST: "请输入一个具体问题。",
    OutcomeCode.HISTORICAL_NOT_SUPPORTED: "W6 只回答现行有效制度，暂不支持历史版本。",
    OutcomeCode.PRIVACY_REQUEST: "本系统不查询个人成绩、学号等个人信息。",
    OutcomeCode.OUT_OF_SCOPE: "本系统只回答华南农业大学的公开制度与办事问题。",
}


MAX_COVERAGE_REPAIR_ROUNDS = 2

_SCOPE_WARNING_PREFIXES = ("AUDIENCE_MISMATCH", "VERSION_MISMATCH", "MIXED_VERSIONS")


def boundary_notes(state: dict[str, Any], reason: OutcomeCode | None) -> list[str]:
    """R7：区分“语料缺失 / 检索遗漏 / 对象版本不一致”，不把服务问题写成缺资料。

    只有确认本轮收录范围内没有对应文件时才提示可能未收录；被适用对象或版本
    剔除的材料说明“学校有规定，但当前条件下不适用”。
    """
    if reason is not OutcomeCode.NO_EVIDENCE:
        return []
    warnings_list = [str(item) for item in (state.get("retrieval_warnings") or [])]
    if any(item.startswith(_SCOPE_WARNING_PREFIXES) for item in warnings_list):
        return [
            "检索到相关材料，但按适用对象或版本被剔除；这不等于学校没有规定，"
            "补充身份、年级或年份后可重试。"
        ]
    if state.get("evidence") or state.get("retrieval_hit_count"):
        return ["检索到材料但未达到引用标准，不能据此认定知识库未收录该事项。"]
    return [
        "本轮收录范围内未命中对应文件；可能是确实未收录，也可能是表述不同未命中，"
        "可换一种说法或提供文件名称。"
    ]


def _message_for(reason: OutcomeCode | None, fallback: str = "") -> str:
    if reason is None:
        return fallback or _DEFAULT_MESSAGES[OutcomeCode.UNVERIFIABLE]
    return (
        QA_MESSAGES.get(reason)
        or _DEFAULT_MESSAGES.get(reason)
        or fallback
        or "本次无法给出可核对的答案。"
    )


def _default_block_types(question: str) -> list[BlockType]:
    """公开知识同时存储为条款和段落；召回后再按相关性与表格配额选择。"""
    return [BlockType.ARTICLE, BlockType.SECTION]


def _append_trace(
    state: GraphState,
    node: str,
    started: float,
    **payload: Any,
) -> list[dict[str, Any]]:
    def scrub(value):
        if isinstance(value, str):
            return redact_sensitive(value, max_chars=2400)
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    payload = scrub(payload)
    trace = list(state.get("trace") or [])
    trace.append(
        {
            "node": node,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **payload,
        }
    )
    return trace


def _pending_question(state: GraphState, plan: Plan, question: str) -> str:
    if not plan.needs_clarification:
        return ""
    if (
        plan.clarification is not None
        and plan.clarification.reason == "AUDIENCE_UNKNOWN"
        and state.get("pending_question")
    ):
        return str(state.get("pending_question"))
    return question


def _flow_citations(state: GraphState) -> list[str]:
    citations: list[str] = []
    flow = state.get("flow_definition")
    if flow is not None:
        for prerequisite in flow.prerequisites:
            citations.extend(prerequisite.citations)
    for item in state.get("checklist") or []:
        if item.status == "not_applicable":
            continue
        citations.extend(item.citations)
    # 混合问题只需要引用依据，不把整批流程步骤的依据全部塞进 Advisor。
    return list(dict.fromkeys(citations))[:12]


def _checklist_scope_supported(
    checklist: list[ChecklistItem],
    evidence: list[Evidence],
    scope: RequestedScope,
) -> bool:
    """Every rendered flow step must cite a source scoped to the asked future year."""
    if not scope.has_future:
        return True
    by_article = {item.article.article_id: item.article for item in evidence}
    by_doc = {item.article.doc_id: item.article for item in evidence}

    def resolve(article_id: str):
        article = by_article.get(article_id)
        if article is not None:
            return article
        article = by_doc.get(article_id)
        if article is not None:
            return article
        return by_doc.get(article_id.split("#", 1)[0])

    for item in checklist:
        if item.status == "not_applicable":
            continue
        if not item.citations:
            return False
        for target in scope.future_targets:
            supported = False
            for citation in item.citations:
                article = resolve(citation)
                if article is None:
                    continue
                if (
                    source_applicability(
                        article,
                        target,
                        question=scope.question,
                        as_of=scope.as_of,
                    ).status
                    == "matched"
                ):
                    supported = True
                    break
            if not supported:
                return False
    return True


def _serialize_records(report) -> list[dict[str, Any]]:
    records = []
    for record in report.hard.records:
        payload = dict(record)
        payload["issues"] = [
            issue.model_dump(mode="json", exclude_none=True)
            if hasattr(issue, "model_dump")
            else issue
            for issue in record.get("issues", [])
        ]
        codes: dict[str, str] = {}
        for issue in report.issues:
            if issue.claim_id and issue.claim_id not in codes:
                codes[issue.claim_id] = issue.code
        if codes.get(record.get("claim_id")):
            payload["semantic_or_flow_issue"] = codes[record.get("claim_id")]
        records.append(payload)
    return records


def _merge_supplement_evidence(
    base: list[Evidence],
    additions: list[Evidence],
    *,
    max_evidence: int,
    token_budget: int,
    cited_keys: set[tuple[str, int]],
) -> tuple[list[Evidence], list[str]]:
    """Bound cumulative evidence while preserving citations and compared objects.

    Priority: cited base evidence -> one fragment per prior document -> one new
    fragment per supplement document -> extra supplement rows -> extra base rows.
    Count and token limits are hard for ordinary evidence. Claim-cited evidence
    and hydrated flow evidence (source=flow, required by the rendered checklist)
    are retained even when over budget and reported separately; deleting them
    would leave dangling citations or an incomplete flow.
    """
    max_evidence = max(1, max_evidence)
    token_budget = max(1, token_budget)
    warnings: list[str] = []
    selected: dict[tuple[str, int], Evidence] = {}
    used = 0

    def try_add(item: Evidence, *, protected: bool = False) -> bool:
        nonlocal used
        if item.key in selected:
            return False
        cost = evidence_cost(item.article)
        over_count = len(selected) >= max_evidence
        over_budget = used + cost > token_budget
        if (over_count or over_budget) and not protected:
            reason = "count" if over_count else "token"
            warnings.append(
                f"SUPPLEMENT_BUDGET_SKIP:{item.article.article_id}:"
                f"p{item.article.part_no}:reason={reason}"
            )
            return False
        selected[item.key] = item
        used += cost
        return True

    for item in base:
        if item.key in cited_keys or getattr(item, "source", "") == "flow":
            try_add(item, protected=True)
    if len(selected) > max_evidence:
        warnings.append(
            f"SUPPLEMENT_PROTECTED_OVER_COUNT:{len(selected)}:{max_evidence}"
        )
    if used > token_budget:
        warnings.append(f"SUPPLEMENT_PROTECTED_OVER_TOKEN:{used}:{token_budget}")

    seen_docs = {item.article.doc_id for item in selected.values()}
    for item in base:
        if item.key in selected or item.article.doc_id in seen_docs:
            continue
        if try_add(item):
            seen_docs.add(item.article.doc_id)
    for item in additions:
        if item.key in selected or item.article.doc_id in seen_docs:
            continue
        if try_add(item):
            seen_docs.add(item.article.doc_id)
    for item in additions:
        if item.key in selected:
            continue
        try_add(item)
    for item in base:
        if item.key in selected:
            continue
        try_add(item)

    merged: list[Evidence] = []
    merged_keys: set[tuple[str, int]] = set()
    for item in [*base, *additions]:
        if item.key in selected and item.key not in merged_keys:
            merged.append(item)
            merged_keys.add(item.key)
    return merged, warnings


def _claim_signature(claim: Claim) -> str:
    """Content + citation semantics; claim_id is deliberately excluded."""
    citations = sorted(
        "|".join(
            [
                citation.article_id,
                str(citation.part_no),
                str(citation.quote_start),
                str(citation.quote_end),
                normalize_text(citation.quote),
            ]
        )
        for citation in claim.citations
    )
    return "|".join([normalize_text(claim.text), *citations])


def _progress_signature(
    missing: list[str],
    evidence: list[Evidence],
    claims: list[Claim],
) -> dict[str, list[str]]:
    """What must change for another supplement round to be worth running.

    Regenerating the exact same verified content under a fresh claim ID is not
    progress; new facts or citations still change the signature.
    """
    return {
        "missing": sorted({str(item) for item in missing}),
        "evidence": sorted(
            f"{item.article.article_id}#p{item.article.part_no}" for item in evidence
        ),
        "claims": sorted({_claim_signature(claim) for claim in claims}),
    }


class GraphNodes:
    def __init__(
        self,
        *,
        planner: Planner,
        retriever: GraphRetriever,
        advisor: Advisor,
        verifier: GraphVerifier,
        catalog: FlowCatalog,
        handoff_config: HandoffConfig | None = None,
        trace_writer: TraceWriter | None = None,
        max_clarify: int = 3,
        max_revise: int = 2,
        max_evidence: int = 8,
        token_budget: int = 4000,
        today: date | None = None,
        coverage_checker: CoverageChecker | None = None,
    ) -> None:
        self._coverage_checker = coverage_checker
        self._max_evidence = max(1, max_evidence)
        self._token_budget = max(1, token_budget)
        self._planner = planner
        self._retriever = retriever
        self._advisor = advisor
        self._verifier = verifier
        self._catalog = catalog
        self._handoff_config = handoff_config
        self._trace_writer = trace_writer
        self._max_clarify = max_clarify
        self._max_revise = max_revise
        self._today = today

    # ----------------------------------------------------------------- planner
    def planner(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        question = state.get("question", "")
        as_of = state.get("as_of") or self._today or date.today()
        context = PlannerContext(
            message=question,
            as_of=as_of,
            session_id=state.get("session_id", ""),
            turn_id=int(state.get("turn_id", 1) or 1),
            active_flow_id=state.get("flow_id"),
            slots=dict(state.get("slots") or {}),
            declined_slots=list(state.get("declined_slots") or []),
            asked_slots=list(state.get("asked_slots") or []),
            pending_slot=state.get("pending_slot"),
            pending_flow_ids=list(state.get("pending_flow_ids") or []),
            clarify_count=int(state.get("clarify_count", 0) or 0),
            audience=state.get("audience"),
            profile=state.get("user_profile"),
            pending_clarification=state.get("clarification"),
            pending_question=state.get("pending_question", ""),
        )
        plan = self._planner.plan(context)
        resolved_question = context.message
        clarify_count = int(state.get("clarify_count", 0) or 0)
        if plan.needs_clarification:
            clarify_count += 1
            if clarify_count > self._max_clarify:
                plan.needs_clarification = False
                plan.clarification = None
                plan.pending_slot = None
                plan.warnings.append("澄清次数超限，转通用路径")
        else:
            clarify_count = 0

        flow_definition = None
        if plan.flow_id:
            try:
                flow_definition = self._catalog.get_flow(plan.flow_id, as_of=as_of)
            except Exception:
                flow_definition = None
            if flow_definition is None:
                plan.warnings.append(f"流程不可用：{plan.flow_id}")
                plan.flow_id = None
                plan.intent = Intent.POLICY_QA
                if not plan.queries:
                    plan.queries = [
                        QueryPlanItem(
                            query_id="q1",
                            text=question,
                            route="policy",
                            filters={},
                        )
                    ]
        slots = plan.slots
        if plan.intent is Intent.POLICY_QA and not slots:
            slots = dict(state.get("slots") or {})

        trace = _append_trace(
            state,
            "planner",
            started,
            plan=plan.trace_payload(),
            # R8：规划模型不可用时保留故障分类与尝试次数。
            provider_error=getattr(self._planner, "last_provider_error", None),
        )
        update: dict[str, Any] = {
            "plan": plan,
            "question": resolved_question,
            "intent": plan.intent,
            "flow_id": plan.flow_id,
            "flow_definition": flow_definition,
            "slots": slots,
            "missing_slots": list(plan.missing_slots),
            "clarification": (
                plan.clarification.model_dump(mode="json") if plan.clarification else None
            ),
            "clarify_count": clarify_count,
            "pending_flow_ids": list(plan.pending_flow_ids),
            "declined_slots": list(plan.declined_slots),
            "asked_slots": list(plan.asked_slots),
            "pending_slot": plan.pending_slot,
            "pending_question": _pending_question(state, plan, question),
            "revision_count": 0,
            "failed_claim_ids": [],
            "revise_issues": [],
            "revise_requirements": [],
            "evidence": [],
            "drafts": [],
            "claims": [],
            "verification": None,
            "verification_issues": [],
            "draft_records": [],
            "probe": {},
            "probe_ms": 0,
            "verification_decision": None,
            "semantic": None,
            "semantic_unchecked": False,
            "decision": None,
            "reason_code": plan.outcome,
            "outcome": (
                {"code": plan.outcome.value, "message": plan.guard_message}
                if plan.outcome
                else None
            ),
            "flow_evaluation": None,
            "flow_status": plan.flow_status,
            "checklist": [],
            "citations": [],
            "coverage": {},
            "coverage_enabled": self._coverage_checker is not None,
            "coverage_checked": False,
            "unit_coverage": [],
            "coverage_missing": [],
            "coverage_repair_pending": False,
            "coverage_repair_count": 0,
            "coverage_repair_last": None,
            "repair_verified_claims": [],
            "unanswered_questions": [],
            "retrieval_warnings": list(plan.warnings),
            "scope_issues": [],
            "final_answer": "",
            "answer_markdown": "",
            "trace": trace,
        }
        return update

    # -------------------------------------------------------------------- flow
    def flow(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        flow = state.get("flow_definition")
        if flow is None:
            trace = _append_trace(state, "flow", started, status="missing")
            return {
                "decision": GraphDecision.ABSTAIN,
                "reason_code": OutcomeCode.NO_EVIDENCE,
                "trace": trace,
            }
        as_of = state.get("as_of") or self._today or date.today()
        try:
            evaluation = self._catalog.evaluate(
                flow,
                state.get("slots") or {},
                as_of=as_of,
                done_step_ids=set(),
                declined_slots=set(state.get("declined_slots") or []),
                asked_slots=set(state.get("asked_slots") or []),
            )
        except Exception as exc:
            trace = _append_trace(
                state,
                "flow",
                started,
                status="error",
                error=__import__(
                    "campusguide.observability.errors", fromlist=["safe_error_summary"]
                ).safe_error_summary(exc),
            )
            return {
                "decision": GraphDecision.ABSTAIN,
                "reason_code": OutcomeCode.TOOL_UNAVAILABLE,
                "checklist": [],
                "trace": trace,
            }
        trace = _append_trace(
            state,
            "flow",
            started,
            flow_id=flow.flow_id,
            status=evaluation.status,
            checklist=len(evaluation.checklist),
            missing_slots=list(evaluation.missing_slots),
            warnings=list(evaluation.warnings),
        )
        return {
            "flow_evaluation": {
                "status": evaluation.status,
                "missing_slots": list(evaluation.missing_slots),
                "warnings": list(evaluation.warnings),
                "eligible": evaluation.eligible,
            },
            "flow_status": evaluation.status,
            "checklist": evaluation.checklist,
            "closed": evaluation.status == "closed",
            "missing_slots": list(evaluation.missing_slots),
            "retrieval_warnings": [
                *(state.get("retrieval_warnings") or []),
                *evaluation.warnings,
            ],
            "trace": trace,
        }

    # --------------------------------------------------------------- retriever
    def retriever(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = state.get("plan")
        if plan is None or not plan.queries:
            trace = _append_trace(state, "retriever", started, queries=0, evidence=0)
            return {
                "decision": GraphDecision.ABSTAIN,
                "reason_code": OutcomeCode.NO_EVIDENCE,
                "trace": trace,
            }
        as_of = state.get("as_of") or self._today or date.today()
        requested_scope = parse_requested_scope(state.get("question", ""), as_of)
        block_types = state.get("block_types") or [BlockType.ARTICLE]
        base = PolicyFilters(
            block_types=list(block_types),
            as_of=as_of,
            audience=(
                [state.get("audience")]
                if state.get("audience") not in (None, Audience.ALL)
                else None
            ),
            profile=state.get("user_profile"),
        )
        retrieve_kwargs: dict[str, Any] = {
            "flow_citations": _flow_citations(state)
        }
        if "search_queries" in inspect.signature(
            self._retriever.retrieve
        ).parameters:
            retrieve_kwargs["search_queries"] = [state.get("question", "")]
        bundle = self._retriever.retrieve(plan, base, **retrieve_kwargs)
        had_evidence = bool(bundle.evidence)
        filtered = filter_evidence_for_request(bundle.evidence, requested_scope)
        bundle.evidence = filtered.evidence
        bundle.issues = [*bundle.issues, *filtered.issues]
        if filtered.reason_code is not None and (
            had_evidence
            or bundle.reason_code in {None, OutcomeCode.NO_EVIDENCE, OutcomeCode.ONLY_REPEALED}
        ):
            bundle.reason_code = filtered.reason_code
        warnings = [*(state.get("retrieval_warnings") or []), *bundle.warnings]
        warnings.extend(f"REQUESTED_SCOPE_DROP:{article_id}" for article_id in filtered.dropped)
        trace = _append_trace(
            state,
            "retriever",
            started,
            queries=[redact_sensitive(item.text) for item in plan.queries],
            evidence=len(bundle.evidence),
            requested_scope={
                "targets": [
                    {"kind": target.kind, "year": target.start_year, "raw": target.raw}
                    for target in requested_scope.targets
                ],
                "future": [target.label() for target in requested_scope.future_targets],
                "dropped": list(filtered.dropped),
            },
            selected_evidence=[
                {
                    "article_id": e.article.article_id,
                    "part_no": e.article.part_no,
                    "doc_id": e.article.doc_id,
                    "version_id": e.article.version_id,
                    "score": e.score,
                    "matched_queries": e.matched_queries,
                }
                for e in bundle.evidence
            ],
            coverage=bundle.coverage,
            warnings=warnings,
            index_version=bundle.index_version,
            partial=bundle.partial,
            fallback=getattr(bundle, "fallback", {}) or {},
        )
        update: dict[str, Any] = {
            "evidence": bundle.evidence,
            "coverage": bundle.coverage,
            "probe": bundle.probe,
            "probe_ms": int(bundle.probe.get("latency_ms", 0) or 0),
            "retrieval_warnings": warnings,
            "scope_issues": bundle.issues,
            "index_version": bundle.index_version,
            "trace": trace,
        }
        if bundle.reason_code is not None and bundle.evidence:
            update["reason_code"] = bundle.reason_code
        if not bundle.evidence:
            update["decision"] = GraphDecision.ABSTAIN
            update["reason_code"] = bundle.reason_code or OutcomeCode.NO_EVIDENCE
            return update
        return update

    # ----------------------------------------------------------------- advisor
    def advisor(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        question = state.get("question", "")
        evidence = state.get("evidence") or []
        audience = state.get("audience")
        flow_context = FLOW_BOUNDARY_PROMPT if state.get("checklist") else ""
        revision_count = int(state.get("revision_count", 0) or 0)
        failed = list(state.get("failed_claim_ids") or [])
        previous = list(state.get("drafts") or [])
        try:
            if revision_count > 0 and failed and previous:
                result = self._advisor.revise(
                    question,
                    evidence,
                    failed_claim_ids=failed,
                    issues=list(state.get("revise_issues") or []),
                    previous_claims=previous,
                    audience=audience,
                    flow_context=flow_context,
                    # 方案 A2：把“必须逐项覆盖”的类型清单带进改写。
                    requirements=list(state.get("revise_requirements") or []),
                )
                drafts = merge_revised_drafts(previous, result.drafts, failed)
                mode = "revise"
            else:
                extra = {}
                if isinstance(self._advisor, Advisor) and state.get("plan"):
                    extra["question_units"] = state["plan"].question_units
                repair_claims = list(state.get("repair_verified_claims") or [])
                generation_question = question
                if state.get("coverage_repair_count", 0):
                    generation_question += (
                        "\n本轮只补充以下遗漏，保留先前已核实内容，不要重复："
                        + str(state.get("coverage_missing") or [])
                    )
                    generation_question += "\n已核实结论：" + str([c.text for c in repair_claims])
                result = self._advisor.generate(
                    generation_question,
                    evidence,
                    audience,
                    flow_context=flow_context,
                    **extra,
                )
                drafts = result.drafts
                mode = "generate"
                if state.get("coverage_repair_count", 0):
                    previous_drafts = [
                        ClaimDraft(
                            claim_id=c.claim_id,
                            text=c.text,
                            kind=c.kind.value,
                            citations=[
                                CitationDraft(article_id=x.article_id, quote=x.quote)
                                for x in c.citations
                            ],
                        )
                        for c in repair_claims
                    ]
                    reserved_ids = {c.claim_id for c in repair_claims if c.claim_id}
                    reserved_ids.update(
                        c.claim_id for c in state.get("claims") or [] if c.claim_id
                    )
                    # Every repair round must mint IDs outside the whole preserved
                    # namespace; reusing supplement_1 in round two makes the hard
                    # validator drop a legitimate new claim as DUPLICATE_CLAIM_ID.
                    repair_round = int(state.get("coverage_repair_count", 0) or 0)
                    fresh = []
                    known_texts = {c.text for c in repair_claims}
                    next_index = 1
                    for draft in drafts:
                        if draft.text in known_texts:
                            continue
                        candidate = f"supplement_{repair_round}_{next_index}"
                        while candidate in reserved_ids:
                            next_index += 1
                            candidate = f"supplement_{repair_round}_{next_index}"
                        reserved_ids.add(candidate)
                        next_index += 1
                        fresh.append(draft.model_copy(update={"claim_id": candidate}))
                    drafts = [*previous_drafts, *fresh]
                    mode = "supplement"
        except (ChatProviderError, AdvisorError) as exc:
            trace = _append_trace(
                state,
                "advisor",
                started,
                mode="error",
                error=safe_error_summary(exc),
                # A7：供应商故障分类、尝试次数与等待，供 trace 回答
                # “哪个节点、几次尝试、是否恢复”，不落密钥。
                provider_error=exc.as_dict() if isinstance(exc, ChatProviderError) else None,
            )
            if state.get("coverage_repair_count") and state.get("repair_verified_claims"):
                return {
                    "claims": state["repair_verified_claims"],
                    "coverage_missing": [
                        *(state.get("coverage_missing") or []),
                        "补答服务不可用，保留此前已核实内容",
                    ],
                    "coverage_checked": True,
                    "decision": GraphDecision.ABSTAIN,
                    "trace": trace,
                }
            return {
                "drafts": [],
                "decision": GraphDecision.ABSTAIN,
                "reason_code": OutcomeCode.GENERATION_FAILED,
                "trace": trace,
            }
        trace = _append_trace(
            state,
            "advisor",
            started,
            mode=mode,
            model=(result.responses[-1].model if result.responses else ""),
            claims=len(drafts),
            prompt_tokens=sum(r.prompt_tokens for r in result.responses),
            completion_tokens=sum(r.completion_tokens for r in result.responses),
            model_latency_ms=sum(r.latency_ms for r in result.responses),
            claim_units={d.claim_id or "": d.unit_ids for d in drafts},
            unanswered_questions=list(result.unanswered_questions),
            quote_repair_attempted=result.quote_repair_attempted,
            quote_repaired=result.quote_repaired,
            quote_repair_remaining=list(result.quote_repair_remaining),
            repaired=result.repaired,
            **({"revision_error": result.revision_error} if result.revision_error else {}),
        )
        update: dict[str, Any] = {
            "drafts": drafts,
            "unanswered_questions": (
                list(state.get("unanswered_questions") or [])
                if mode == "revise"
                else list(result.unanswered_questions)
            ),
            "history_claims": [
                {
                    "claim_id": draft.claim_id,
                    "text": redact_sensitive(draft.text),
                    "kind": draft.kind,
                }
                for draft in drafts
            ],
            "trace": trace,
        }
        if result.revision_error:
            # 严格契约连续失败已耗尽本轮的修订尝试；后续 Verifier 只丢弃
            # 仍失败的 claim，不再重复改写已通过项。
            update["revision_count"] = max(revision_count, self._max_revise)
        return update

    # ---------------------------------------------------------------- verifier
    def verifier(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        drafts = list(state.get("drafts") or [])
        evidence = state.get("evidence") or []
        as_of = state.get("as_of") or self._today or date.today()
        verification_extra = {}
        if isinstance(self._verifier, GraphVerifier):
            verification_extra["previously_verified"] = list(
                state.get("repair_verified_claims") or []
            )
            verification_extra["question"] = state.get("question", "")
            verification_extra["retrieval_warnings"] = list(state.get("retrieval_warnings") or [])
            verification_extra["as_of"] = as_of
        report = self._verifier.verify(
            drafts,
            evidence,
            scope_issues=list(state.get("scope_issues") or []),
            checklist=list(state.get("checklist") or []),
            revision_count=int(state.get("revision_count", 0) or 0),
            clarify_count=int(state.get("clarify_count", 0) or 0),
            max_clarify=self._max_clarify,
            **verification_extra,
        )
        semantic_payload = None
        if report.semantic is not None:
            semantic_payload = {
                "labels": {
                    claim_id: label.value for claim_id, label in report.semantic.labels.items()
                },
                "details": dict(report.semantic.details),
                "model": report.semantic.model,
                "latency_ms": report.semantic.latency_ms,
                "checked": report.semantic.checked,
                "prompt_tokens": report.semantic.prompt_tokens,
                "completion_tokens": report.semantic.completion_tokens,
                "unavailable": report.semantic.unavailable,
                "error": report.semantic.error,
            }
        trace = _append_trace(
            state,
            "verifier",
            started,
            decision=report.decision.value,
            failed_claim_ids=list(report.failed_claim_ids),
            semantic=semantic_payload,
            semantic_unchecked=report.semantic_unchecked,
            detail=report.detail,
            issues=[i.model_dump(mode="json", exclude_none=True) for i in report.issues],
            draft_records=_serialize_records(report),
        )
        update: dict[str, Any] = {
            "decision": report.decision,
            "semantic": semantic_payload,
            "semantic_unchecked": report.semantic_unchecked,
            "trace": trace,
        }
        if report.decision is GraphDecision.REVISE:
            update["revision_count"] = int(state.get("revision_count", 0) or 0) + 1
            update["failed_claim_ids"] = list(report.failed_claim_ids)
            update["revise_issues"] = list(report.revise_issues)
            update["revise_requirements"] = list(report.revise_requirements)
            return update
        if report.decision is GraphDecision.CLARIFY:
            update["clarification"] = {
                "reason": "VERSION_UNKNOWN",
                "question": report.detail,
            }
            return update
        update["failed_claim_ids"] = list(report.failed_claim_ids)
        update["verification_issues"] = [
            issue.model_dump(mode="json", exclude_none=True) for issue in report.issues
        ]
        update["draft_records"] = _serialize_records(report)
        if report.verification is not None:
            update["verification"] = report.verification.model_dump(mode="json")
            update["verification_decision"] = report.verification.decision
            update["claims"] = report.verification.claims
            update["reason_code"] = report.verification.reason_code
        else:
            update["claims"] = []
            update["verification_decision"] = VerificationDecision.ABSTAIN
            update["reason_code"] = report.reason_code or OutcomeCode.UNVERIFIABLE
        return update

    # ------------------------------------------------------ answer completeness
    def coverage(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = state.get("plan")
        if self._coverage_checker is None or plan is None:
            return {"coverage_repair_pending": False}
        audit = self._coverage_checker.check(
            state.get("question", ""),
            plan.question_units,
            list(state.get("claims") or []),
            list(state.get("evidence") or []),
            list(state.get("checklist") or []),
            retrieval_warnings=list(state.get("retrieval_warnings") or []),
        )
        contradicted = (
            "contradicted" in ((state.get("semantic") or {}).get("labels") or {}).values()
        )
        previous = state.get("coverage_repair_last") or {}
        signature = _progress_signature(
            audit.missing,
            list(state.get("evidence") or []),
            list(state.get("claims") or []),
        )
        # A round that produced neither a new supported claim nor a smaller
        # missing set would repeat the same queries and generation verbatim.
        # Keep the honest partial instead of spending the second round.
        converged = bool(
            state.get("coverage_repair_count", 0)
            and previous.get("round") == state.get("coverage_repair_count")
            and previous.get("signature") == signature
        )
        repair = bool(
            audit.missing
            and not audit.unavailable
            and not contradicted
            and not converged
            and state.get("coverage_repair_count", 0) < MAX_COVERAGE_REPAIR_ROUNDS
            and any(
                u.get("status") in {"generation_missing", "missing_evidence"} for u in audit.units
            )
        )
        return {
            "coverage_checked": True,
            "unit_coverage": audit.units,
            "coverage_missing": audit.missing,
            "coverage_repair_pending": repair,
            "trace": _append_trace(
                state,
                "coverage",
                started,
                units=audit.units,
                missing=audit.missing,
                unavailable=audit.unavailable,
                error=audit.error,
                provider_error=(
                    {"kind": audit.error_kind, "attempts": audit.error_attempts}
                    if audit.error_kind
                    else None
                ),
                prompt_tokens=audit.prompt_tokens,
                completion_tokens=audit.completion_tokens,
                model_latency_ms=audit.latency_ms,
                repair_pending=repair,
                repair_converged=converged,
            ),
        }

    def supplement(self, state: GraphState) -> dict[str, Any]:
        """R4：按单元补查，最多两轮（首轮 3 条、次轮 2 条），不替换已核实内容。"""
        started = time.perf_counter()
        round_index = int(state.get("coverage_repair_count", 0))
        if round_index >= MAX_COVERAGE_REPAIR_ROUNDS:
            return {"coverage_repair_pending": False}
        query_budget = 3 if round_index == 0 else 2
        plan = state.get("plan")
        if plan is None:
            return {"coverage_repair_pending": False}
        missing_ids = {
            row["unit_id"]
            for row in state.get("unit_coverage") or []
            if row["status"] != "answered"
        }
        queries = []
        seen = set()
        for unit in plan.question_units:
            if unit.unit_id not in missing_ids:
                continue
            row_missing = next(
                (
                    row.get("missing_items") or []
                    for row in (state.get("unit_coverage") or [])
                    if row["unit_id"] == unit.unit_id
                ),
                [],
            )
            focus = " ".join(
                [
                    str(unit.cohort) + "级" if unit.cohort else "",
                    unit.subject,
                    "、".join(unit.requested_aspects),
                ]
            )
            # The original question provides unnamed subjects (e.g. committee names).
            text = f"{unit.original_span[:100]} {focus} {'、'.join(row_missing)[:70]}"[:200]
            if text in seen:
                continue
            seen.add(text)
            filters = next((q.filters for q in plan.queries if unit.unit_id in q.unit_ids), {})
            queries.append(
                QueryPlanItem(
                    query_id=f"repair_q{round_index + 1}_{len(queries) + 1}",
                    text=text,
                    filters=filters,
                    unit_ids=[unit.unit_id],
                )
            )
            if len(queries) == query_budget:
                break
        base = PolicyFilters(
            block_types=state.get("block_types") or _default_block_types(state.get("question", "")),
            as_of=state.get("as_of") or self._today or date.today(),
            audience=[state["audience"]]
            if state.get("audience") not in (None, Audience.ALL)
            else None,
            profile=state.get("user_profile"),
        )
        bundle = self._retriever.retrieve(Plan(queries=queries), base) if queries else None
        requested_scope = parse_requested_scope(state.get("question", ""), base.as_of)
        requested_dropped: list[str] = []
        if bundle is not None:
            filtered = filter_evidence_for_request(bundle.evidence, requested_scope)
            bundle.evidence = filtered.evidence
            requested_dropped = filtered.dropped
            bundle.warnings.extend(
                f"REQUESTED_SCOPE_DROP:{article_id}" for article_id in filtered.dropped
            )
        base_evidence = list(state.get("evidence") or [])
        additions: list[Evidence] = []
        known = {e.key for e in base_evidence}
        if bundle is not None:
            for hit in bundle.evidence:
                if hit.key not in known:
                    additions.append(hit)
                    known.add(hit.key)
        cited_keys = {
            (citation.article_id, citation.part_no)
            for claim in state.get("claims") or []
            for citation in claim.citations
        }
        evidence, budget_warnings = _merge_supplement_evidence(
            base_evidence,
            additions,
            max_evidence=getattr(self, "_max_evidence", 8),
            token_budget=getattr(self, "_token_budget", 4000),
            cited_keys=cited_keys,
        )
        for i, hit in enumerate(evidence, 1):
            hit.rank = i
        return {
            "coverage_repair_count": round_index + 1,
            "coverage_repair_pending": False,
            "coverage_checked": False,
            "coverage_repair_last": {
                "round": round_index + 1,
                # Baseline before this round's add-only merge. A round that
                # added evidence is progress even when claims and missing
                # items are unchanged, so the next coverage pass must not
                # report convergence against the post-merge state.
                "signature": _progress_signature(
                    list(state.get("coverage_missing") or []),
                    base_evidence,
                    list(state.get("claims") or []),
                ),
                "added_evidence": len(evidence) - len(base_evidence),
            },
            "repair_verified_claims": list(state.get("claims") or []),
            "evidence": evidence,
            "decision": None,
            # 补查不能重置改写预算：否则每轮补查都会再触发两次“生成→核对”，
            # 两轮下来超过 LangGraph 的 20 步上限，整轮回合被丢弃（049）。
            "failed_claim_ids": [],
            "revise_issues": [],
            "revise_requirements": [],
            "retrieval_warnings": [
                *(state.get("retrieval_warnings") or []),
                *(bundle.warnings if bundle else []),
                *budget_warnings,
            ],
            "trace": _append_trace(
                state,
                "supplement",
                started,
                queries=[q.model_dump(mode="json") for q in queries],
                added_evidence=len(evidence) - len(base_evidence),
                evidence_count=len(evidence),
                budget_warnings=budget_warnings,
                preserved_claims=[c.claim_id for c in state.get("claims") or []],
                requested_scope_dropped=requested_dropped,
                warnings=bundle.warnings if bundle else [],
            ),
        }

    # ---------------------------------------------------------------- finalize
    def finalize(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = state.get("plan")
        flow = state.get("flow_definition")
        checklist: list[ChecklistItem] = list(state.get("checklist") or [])
        claims: list[Claim] = list(state.get("claims") or [])
        # R6/A1：合并去重、同一事项只留一致表述，避免“未收录”与明确规则并存。
        # 冲突不再按引用数或先后择一，而是并列保留并提示核实；信息性去重
        # 日志不进入完整性判断。
        merged = merge_claims(claims)
        claims = merged.claims
        merge_conflicts = [note.detail for note in merged.substantive_notes]
        retained_claim_ids = {claim.claim_id for claim in claims}
        replacement_claim_ids = merged.replacement_claim_ids
        unit_coverage = [dict(row) for row in (state.get("unit_coverage") or [])]

        def _valid_flow_support_ids(
            rendered_checklist: list[ChecklistItem],
        ) -> set[str]:
            if flow is None:
                return set()
            definition_ids = {step.id for step in flow.steps}
            hydrated_ids = {
                item.article.article_id for item in (state.get("evidence") or [])
            }
            return {
                f"flow:{item.step_id}"
                for item in rendered_checklist
                if item.step_id in definition_ids
                and item.status != "not_applicable"
                and item.citations
                and set(item.citations) <= hydrated_ids
            }

        if state.get("coverage_checked"):
            # 合并改变了 claim 集合，不能沿用合并前的覆盖状态（A1）。
            # 重复 claim 的引用先映射到实际保留 ID；无映射的删除不得改指其他 claim。
            # flow: 支持必须对应真实流程步骤、实际渲染的 checklist 和已水合依据，
            # 不能仅凭前缀保留。
            valid_flow_ids = _valid_flow_support_ids(checklist)
            refreshed = []
            for row in unit_coverage:
                supported: list[str] = []
                for claim_id in row.get("claim_ids") or []:
                    mapped_id = replacement_claim_ids.get(claim_id, claim_id)
                    if mapped_id not in retained_claim_ids and mapped_id not in valid_flow_ids:
                        continue
                    if mapped_id not in supported:
                        supported.append(mapped_id)
                row["claim_ids"] = supported
                if row.get("status") == "answered" and not supported:
                    row["status"] = "generation_missing"
                refreshed.append(row)
            unit_coverage = refreshed
        reason = state.get("reason_code")
        as_of = state.get("as_of") or self._today or date.today()
        requested_scope = parse_requested_scope(state.get("question", ""), as_of)

        def reason_message() -> str:
            if reason is OutcomeCode.VERSION_MISMATCH and requested_scope.targets:
                return requested_scope_message(requested_scope)
            return _message_for(reason)

        semantic_unchecked = bool(state.get("semantic_unchecked"))
        missing_slots = list(state.get("missing_slots") or [])
        handoff = resolve_handoff(
            self._handoff_config,
            flow=flow,
            reason=reason,
        )
        outcome_code = "UNVERIFIABLE"
        answer = ""
        citations: list[dict[str, Any]] = []
        rendered_claims: list[Claim] = []
        extra_warnings = self._coverage_warnings(state)
        for part in (
            state.get("coverage_missing")
            if state.get("coverage_checked")
            else state.get("unanswered_questions")
        ) or []:
            safe_part = redact_sensitive(str(part), max_chars=120)
            status = next(
                (
                    r["status"]
                    for r in (state.get("unit_coverage") or [])
                    if str(part) in (r.get("missing_items") or [])
                ),
                "generation_missing",
            )
            explanation = {
                "missing_evidence": "缺少直接证据",
                "verification_failed": "尚未通过核对",
                "clarification_needed": "需要补充查询条件",
            }.get(status, "已找到相关材料，但尚未完整答出")
            extra_warnings.append(f"尚缺“{safe_part}”：{explanation}。")
        # 冲突提示只用于展示，不改变完整性状态（只有实质缺项才算不完整）。
        display_warnings = [*extra_warnings, *merge_conflicts]
        checklist_omitted_for_scope = False
        if (
            requested_scope.has_future
            and claims
            and checklist
            and not _checklist_scope_supported(
                checklist,
                state.get("evidence") or [],
                requested_scope,
            )
        ):
            checklist_omitted_for_scope = True
            extra_warnings.append(
                "流程清单来源未证明适用于所问年度/学年，已省略流程步骤。"
            )
            display_warnings = [*extra_warnings, *merge_conflicts]
            checklist = []
        closed = bool(state.get("closed"))
        coverage_incomplete = bool(
            state.get("coverage_checked")
            and any(row.get("status") != "answered" for row in unit_coverage)
        )
        incomplete = bool(
            extra_warnings or (plan and plan.dropped_queries) or coverage_incomplete
        )

        if plan is not None and plan.is_guard:
            outcome_code = plan.outcome.value if plan.outcome else "INVALID_REQUEST"
            answer = finalize_abstain(
                message=plan.guard_message or _message_for(plan.outcome),
                handoff=handoff,
                semantic_unchecked=semantic_unchecked,
            )
        elif plan is not None and plan.needs_clarification and plan.clarification is not None:
            outcome_code = "CLARIFY"
            lines = [plan.clarification.question]
            if plan.clarification.reason == "SLOT_MISSING":
                lines.append("可以直接回答；不想说也可以说“跳过”。")
            elif plan.clarification.allowed_values:
                lines.append("可选：" + "、".join(plan.clarification.allowed_values))
            answer = "\n".join(lines)
        elif claims and flow is not None and checklist:
            verification = state.get("verification_decision")
            partial = (
                incomplete
                if state.get("coverage_checked")
                else (verification is VerificationDecision.PARTIAL or incomplete)
            )
            mixed = finalize_mixed(
                claims,
                flow=flow,
                checklist=checklist,
                status=str(state.get("flow_status") or "unknown"),
                handoff=handoff,
                closed=closed,
                partial=partial,
                dropped_count=len(state.get("failed_claim_ids") or []),
                semantic_unchecked=semantic_unchecked,
                extra_lines=display_warnings,
            )
            answer = mixed.answer_markdown
            citations = [item.model_dump(mode="json") for item in mixed.citations]
            rendered_claims = claims
            outcome_code = "PARTIAL_ANSWER" if partial else "ANSWERED"
        elif claims:
            verification = state.get("verification_decision")
            partial = (
                incomplete
                if state.get("coverage_checked")
                else (verification is VerificationDecision.PARTIAL or incomplete)
            )
            finished = finalize(
                claims,
                partial=partial,
                dropped_count=len(state.get("failed_claim_ids") or []),
                semantic_unchecked=semantic_unchecked,
            )
            answer = finished.answer_markdown
            if display_warnings:
                marker = "\n---\n" + DISCLAIMER
                warning_text = "\n".join(f"> {item}" for item in display_warnings)
                if answer.endswith(marker):
                    answer = (
                        answer[: -len(marker)].rstrip() + "\n\n" + warning_text + "\n\n" + marker
                    )
            citations = [item.model_dump(mode="json") for item in finished.citations]
            rendered_claims = claims
            outcome_code = "PARTIAL_ANSWER" if partial else "ANSWERED"
        elif (
            flow is not None
            and requested_scope.has_future
            and reason in {None, OutcomeCode.VERSION_MISMATCH}
        ):
            outcome_code = "VERSION_MISMATCH"
            answer = finalize_abstain(
                message=requested_scope_message(requested_scope),
                handoff=handoff,
                missing_slots=missing_slots,
                warnings=[
                    *display_warnings,
                    "流程清单来自当前批次，未证明适用于所问年度/学年。",
                ],
                semantic_unchecked=semantic_unchecked,
            )
        elif flow is not None and checklist:
            extra = list(display_warnings)
            if reason is not None:
                extra.append(f"> {reason_message()}")
            answer = finalize_flow(
                flow,
                checklist,
                status=str(state.get("flow_status") or "unknown"),
                handoff=handoff,
                closed=closed,
                extra_lines=extra,
            )
            missing_facts = bool(plan and plan.queries and (incomplete or not claims))
            outcome_code = (
                "PARTIAL_ANSWER" if missing_facts or (missing_slots and not closed) else "ANSWERED"
            )
        elif flow is not None and plan is not None and plan.intent is Intent.MIXED:
            answer = finalize_flow_without_claims(
                flow=flow,
                checklist=[],
                status=str(state.get("flow_status") or "unknown"),
                handoff=handoff,
                closed=closed,
                reason_message=reason_message(),
            )
            outcome_code = reason.value if reason else "UNVERIFIABLE"
        else:
            outcome_code = reason.value if reason else "UNVERIFIABLE"
            answer = finalize_abstain(
                message=reason_message(),
                handoff=handoff,
                missing_slots=missing_slots,
                warnings=[*display_warnings, *boundary_notes(state, reason)],
                semantic_unchecked=semantic_unchecked,
            )

        # 渲染后才确定实际交付的支持集合：claims 取 rendered_claims，
        # flow 只认最终本地 checklist 中仍然有效且实际渲染的步骤。
        # 被 guard/clarify/scope 省略掉的步骤不能继续给 unit 提供 answered 支持。
        rendered_checklist = (
            list(checklist)
            if checklist
            and flow is not None
            and outcome_code in {"ANSWERED", "PARTIAL_ANSWER"}
            and not (plan is not None and plan.is_guard)
            else []
        )
        rendered_support_ids = {claim.claim_id for claim in rendered_claims}
        rendered_support_ids.update(_valid_flow_support_ids(rendered_checklist))
        reconciled: list[dict[str, Any]] = []
        for row in unit_coverage:
            previous_ids = list(row.get("claim_ids") or [])
            supported = [
                claim_id for claim_id in previous_ids if claim_id in rendered_support_ids
            ]
            row["claim_ids"] = supported
            if row.get("status") == "answered" and not supported:
                row["status"] = (
                    "verification_failed" if previous_ids else "generation_missing"
                )
            reconciled.append(row)
        unit_coverage = reconciled
        if outcome_code == "ANSWERED" and any(
            row.get("status") != "answered" for row in unit_coverage
        ):
            outcome_code = "PARTIAL_ANSWER"

        trace = _append_trace(
            state,
            "finalize",
            started,
            outcome=outcome_code,
            citations=len(citations),
            checklist=len(rendered_checklist),
            handoff=handoff.as_dict(),
            semantic_unchecked=semantic_unchecked,
            requested_scope={
                "targets": [target.label() for target in requested_scope.targets],
                "future": [target.label() for target in requested_scope.future_targets],
                "checklist_omitted": checklist_omitted_for_scope,
            },
            coverage=state.get("coverage") or {},
            unit_coverage=unit_coverage,
            coverage_repair_count=state.get("coverage_repair_count", 0),
        )
        duration_ms = int(
            (time.perf_counter() - float(state.get("_started_perf", time.perf_counter()))) * 1000
        )
        result: dict[str, Any] = {
            "final_answer": answer,
            "answer_markdown": answer,
            # checkpoint 只保留脱敏后的当前问题，不落原始隐私文本。
            "question": redact_sensitive(state.get("question", "")),
            "citations": citations,
            "claims": rendered_claims,
            "checklist": rendered_checklist,
            "unit_coverage": unit_coverage,
            "handoff": handoff.as_dict(),
            "outcome": {"code": outcome_code, "message": ""},
            "trace": trace,
            "duration_ms": duration_ms,
        }
        if self._trace_writer is not None:
            self._trace_writer.write(
                {
                    "trace_id": state.get("session_id", ""),
                    "session_id": state.get("session_id", ""),
                    "turn_id": state.get("turn_id", 1),
                    "started_at": state.get("started_at", ""),
                    "duration_ms": duration_ms,
                    "question_redacted": redact_sensitive(state.get("question", "")),
                    "outcome": outcome_code,
                    "graph_version": GRAPH_VERSION,
                    "graph_trace": trace,
                    "coverage": state.get("coverage") or {},
                    "semantic": state.get("semantic"),
                    "revision_count": state.get("revision_count", 0),
                    "clarify_count": state.get("clarify_count", 0),
                    "planner_source": (plan.planner_source if plan is not None else None),
                }
            )
        return result

    @staticmethod
    def _coverage_warnings(state: GraphState) -> list[str]:
        plan = state.get("plan")
        if plan is None or state.get("coverage_checked"):
            return []
        coverage = state.get("coverage") or {}
        warnings = []
        for item in plan.queries:
            if coverage.get(item.query_id):
                continue
            warnings.append(f"子问题“{item.text}”未检索到可引用的依据，未作答。")
        return warnings


def route_after_planner(state: GraphState) -> str:
    plan = state.get("plan")
    if plan is None or plan.is_guard or plan.needs_clarification:
        return "finalize"
    if plan.flow_id:
        return "flow"
    if plan.queries:
        return "retriever"
    return "finalize"


def route_after_flow(state: GraphState) -> str:
    if state.get("decision") is GraphDecision.ABSTAIN:
        return "finalize"
    plan = state.get("plan")
    if plan is not None and (plan.intent is Intent.MIXED or plan.queries):
        return "retriever"
    return "finalize"


def route_after_retriever(state: GraphState) -> str:
    if state.get("decision") is GraphDecision.ABSTAIN:
        return "finalize"
    if state.get("evidence"):
        return "advisor"
    return "finalize"


def route_after_advisor(state: GraphState) -> str:
    if state.get("decision") is GraphDecision.ABSTAIN:
        return "finalize"
    return "verifier"


def route_after_verifier(state: GraphState) -> str:
    decision = state.get("decision")
    if decision is GraphDecision.REVISE:
        return "advisor"
    if decision is GraphDecision.CLARIFY:
        return "planner"
    if state.get("coverage_enabled") and state.get("evidence"):
        return "coverage"
    return "finalize"


def route_after_coverage(state: GraphState) -> str:
    return "supplement" if state.get("coverage_repair_pending") else "finalize"


def build_graph(nodes: GraphNodes, *, checkpointer: Any | None = None):
    graph = StateGraph(GraphState)
    graph.add_node("planner", nodes.planner)
    graph.add_node("flow", nodes.flow)
    graph.add_node("retriever", nodes.retriever)
    graph.add_node("advisor", nodes.advisor)
    graph.add_node("verifier", nodes.verifier)
    graph.add_node("coverage", getattr(nodes, "coverage", lambda state: {}))
    graph.add_node("supplement", getattr(nodes, "supplement", lambda state: {}))
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"flow": "flow", "retriever": "retriever", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "flow",
        route_after_flow,
        {"retriever": "retriever", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "retriever",
        route_after_retriever,
        {"advisor": "advisor", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "advisor",
        route_after_advisor,
        {"verifier": "verifier", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "verifier",
        route_after_verifier,
        {
            "advisor": "advisor",
            "planner": "planner",
            "coverage": "coverage",
            "finalize": "finalize",
        },
    )
    graph.add_conditional_edges(
        "coverage", route_after_coverage, {"supplement": "supplement", "finalize": "finalize"}
    )
    graph.add_edge("supplement", "advisor")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _msgpack_classes() -> list[type]:
    """GraphState 中可能出现的本项目类型；显式 allowlist 避免 checkpoint 警告。"""
    from campusguide.agents import advisor as advisor_mod
    from campusguide.agents import plan as plan_mod
    from campusguide.domain import audience as audience_mod
    from campusguide.domain import enums as enums_mod
    from campusguide.domain import models as models_mod
    from campusguide.domain import profiles as profiles_mod

    classes: list[type] = []
    for module in (
        advisor_mod,
        plan_mod,
        audience_mod,
        enums_mod,
        models_mod,
        profiles_mod,
    ):
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == module.__name__:
                classes.append(value)
    return classes


@dataclass(frozen=True)
class GraphStreamEvent:
    """图流事件：progress 只有节点名；final 才携带完整 state。"""

    kind: Literal["progress", "final"]
    node: str = ""
    state: dict[str, Any] | None = None


class GraphRunner:
    def __init__(
        self,
        nodes: GraphNodes,
        *,
        checkpointer: Any | None = None,
        ttl_hours: int = 24,
    ) -> None:
        # GraphState 内含 Pydantic 领域对象；MemorySaver 默认只支持 msgpack
        # 基础类型，因此在本地内存 checkpoint 中启用 pickle fallback。
        self._checkpointer = checkpointer or MemorySaver(
            serde=JsonPlusSerializer(
                pickle_fallback=True,
                allowed_msgpack_modules=_msgpack_classes(),
            )
        )
        self._graph = build_graph(nodes, checkpointer=self._checkpointer)
        self._ttl_hours = max(1, ttl_hours)
        self._last_seen: dict[str, datetime] = {}

    @property
    def graph(self):
        return self._graph

    def _prepare(
        self,
        session_id: str,
        question: str,
        *,
        as_of: date | None = None,
        audience: Audience | None = None,
        profile: UserProfile | None = None,
        block_types: list[BlockType] | None = None,
    ) -> tuple[GraphState, dict[str, Any]]:
        started_at = datetime.now(UTC)
        last = self._last_seen.get(session_id)
        if last is not None and (started_at - last).total_seconds() > self._ttl_hours * 3600:
            self.reset(session_id)
        self._last_seen[session_id] = started_at
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 20,
        }
        try:
            snapshot = self._graph.get_state(config)
            previous = snapshot.values if snapshot else {}
        except Exception:
            previous = {}
        turn_id = int(previous.get("turn_id", 0) or 0) + 1 if previous else 1
        state: GraphState = {
            "session_id": session_id,
            "turn_id": turn_id,
            "question": question,
            "as_of": as_of or date.today(),
            "audience": audience,
            "user_profile": profile,
            "block_types": list(block_types or _default_block_types(question)),
            "started_at": started_at.isoformat(timespec="seconds"),
            "_started_perf": time.perf_counter(),
            "trace": [],
            # 每轮清空，避免上一轮答案/证据被 checkpoint 复用。
            "plan": None,
            "evidence": [],
            "drafts": [],
            "claims": [],
            "verification": None,
            "verification_issues": [],
            "draft_records": [],
            "probe": {},
            "probe_ms": 0,
            "verification_decision": None,
            "decision": None,
            "reason_code": None,
            "outcome": None,
            "clarification": None,
            "semantic": None,
            "semantic_unchecked": False,
            "checklist": [],
            "flow_definition": None,
            "flow_evaluation": None,
            "flow_status": None,
            "citations": [],
            "coverage": {},
            "unanswered_questions": [],
            "retrieval_warnings": [],
            "scope_issues": [],
            "final_answer": "",
            "answer_markdown": "",
            "failed_claim_ids": [],
            "revise_issues": [],
            "revise_requirements": [],
            "revision_count": 0,
        }
        return state, config

    def invoke(
        self,
        session_id: str,
        question: str,
        *,
        as_of: date | None = None,
        audience: Audience | None = None,
        profile: UserProfile | None = None,
        block_types: list[BlockType] | None = None,
    ) -> GraphState:
        state, config = self._prepare(
            session_id,
            question,
            as_of=as_of,
            audience=audience,
            profile=profile,
            block_types=block_types,
        )
        return self._graph.invoke(state, config)  # type: ignore[return-value]

    def stream(
        self,
        session_id: str,
        question: str,
        *,
        as_of: date | None = None,
        audience: Audience | None = None,
        profile: UserProfile | None = None,
        block_types: list[BlockType] | None = None,
    ) -> Iterator[GraphStreamEvent]:
        """只产出节点完成事件，不产出未核对答案 token。"""
        state, config = self._prepare(
            session_id,
            question,
            as_of=as_of,
            audience=audience,
            profile=profile,
            block_types=block_types,
        )
        node_names = {"planner", "flow", "retriever", "advisor", "verifier", "finalize"}
        for chunk in self._graph.stream(state, config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node in chunk:
                if node in node_names:
                    yield GraphStreamEvent(kind="progress", node=str(node))
        snapshot = self._graph.get_state(config)
        final_state = dict(snapshot.values) if snapshot and snapshot.values else {}
        yield GraphStreamEvent(kind="final", state=final_state)

    def reset(self, session_id: str) -> None:
        self._last_seen.pop(session_id, None)
        delete = getattr(self._checkpointer, "delete_thread", None)
        if callable(delete):
            delete(session_id)


__all__ = [
    "GRAPH_VERSION",
    "GraphNodes",
    "GraphRunner",
    "GraphStreamEvent",
    "build_graph",
    "route_after_advisor",
    "route_after_flow",
    "route_after_planner",
    "route_after_retriever",
    "route_after_verifier",
]

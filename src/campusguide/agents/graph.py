"""W6 完整四角色图：Planner → Flow / Retriever → Advisor → Verifier → Finalize。

节点只通过 ``GraphState`` 交互；FlowExecutor 不调用 LLM，只执行 W5
``evaluate_flow``。revise 回到 Advisor（≤2），clarify 回到 Planner（≤3），
其余路径统一进 Finalize。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from campusguide.agents.advisor import (
    FLOW_BOUNDARY_PROMPT,
    Advisor,
    AdvisorError,
    merge_revised_drafts,
)
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
from campusguide.domain.models import ChecklistItem, Claim, PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.flows.catalog import FlowCatalog
from campusguide.handoff import resolve_handoff
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
from campusguide.qa.service import _MESSAGES as QA_MESSAGES

GRAPH_VERSION = "w6-2026-10-25"

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


def _message_for(reason: OutcomeCode | None, fallback: str = "") -> str:
    if reason is None:
        return fallback or _DEFAULT_MESSAGES[OutcomeCode.UNVERIFIABLE]
    return (
        QA_MESSAGES.get(reason)
        or _DEFAULT_MESSAGES.get(reason)
        or fallback
        or "本次无法给出可核对的答案。"
    )


def _append_trace(
    state: GraphState,
    node: str,
    started: float,
    **payload: Any,
) -> list[dict[str, Any]]:
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
        today: date | None = None,
    ) -> None:
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
        )
        update: dict[str, Any] = {
            "plan": plan,
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
                error=type(exc).__name__,
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
        bundle = self._retriever.retrieve(
            plan,
            base,
            flow_citations=_flow_citations(state),
        )
        warnings = [*(state.get("retrieval_warnings") or []), *bundle.warnings]
        trace = _append_trace(
            state,
            "retriever",
            started,
            queries=[redact_sensitive(item.text) for item in plan.queries],
            evidence=len(bundle.evidence),
            coverage=bundle.coverage,
            warnings=warnings,
            index_version=bundle.index_version,
            partial=bundle.partial,
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
                )
                drafts = merge_revised_drafts(previous, result.drafts, failed)
                mode = "revise"
            else:
                result = self._advisor.generate(
                    question,
                    evidence,
                    audience,
                    flow_context=flow_context,
                )
                drafts = result.drafts
                mode = "generate"
        except (ChatProviderError, AdvisorError) as exc:
            trace = _append_trace(
                state,
                "advisor",
                started,
                mode="error",
                error=type(exc).__name__,
            )
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
            quote_repair_attempted=result.quote_repair_attempted,
            quote_repaired=result.quote_repaired,
            quote_repair_remaining=list(result.quote_repair_remaining),
            repaired=result.repaired,
        )
        return {
            "drafts": drafts,
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

    # ---------------------------------------------------------------- verifier
    def verifier(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        drafts = list(state.get("drafts") or [])
        evidence = state.get("evidence") or []
        report = self._verifier.verify(
            drafts,
            evidence,
            scope_issues=list(state.get("scope_issues") or []),
            checklist=list(state.get("checklist") or []),
            revision_count=int(state.get("revision_count", 0) or 0),
            clarify_count=int(state.get("clarify_count", 0) or 0),
            max_clarify=self._max_clarify,
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

    # ---------------------------------------------------------------- finalize
    def finalize(self, state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = state.get("plan")
        flow = state.get("flow_definition")
        checklist: list[ChecklistItem] = list(state.get("checklist") or [])
        claims: list[Claim] = list(state.get("claims") or [])
        reason = state.get("reason_code")
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
        extra_warnings = self._coverage_warnings(state)
        closed = bool(state.get("closed"))

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
            partial = verification is VerificationDecision.PARTIAL
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
                extra_lines=extra_warnings,
            )
            answer = mixed.answer_markdown
            citations = [item.model_dump(mode="json") for item in mixed.citations]
            outcome_code = "PARTIAL_ANSWER" if partial else "ANSWERED"
        elif claims:
            verification = state.get("verification_decision")
            partial = verification is VerificationDecision.PARTIAL
            finished = finalize(
                claims,
                partial=partial,
                dropped_count=len(state.get("failed_claim_ids") or []),
                semantic_unchecked=semantic_unchecked,
            )
            answer = finished.answer_markdown
            if extra_warnings:
                marker = "\n---\n" + DISCLAIMER
                warning_text = "\n".join(f"> {item}" for item in extra_warnings)
                if answer.endswith(marker):
                    answer = (
                        answer[: -len(marker)].rstrip() + "\n\n" + warning_text + "\n\n" + marker
                    )
            citations = [item.model_dump(mode="json") for item in finished.citations]
            outcome_code = "PARTIAL_ANSWER" if partial else "ANSWERED"
        elif flow is not None and checklist:
            extra = list(extra_warnings)
            if reason is not None:
                extra.append(f"> {_message_for(reason)}")
            answer = finalize_flow(
                flow,
                checklist,
                status=str(state.get("flow_status") or "unknown"),
                handoff=handoff,
                closed=closed,
                extra_lines=extra,
            )
            outcome_code = "PARTIAL_ANSWER" if missing_slots and not closed else "ANSWERED"
        elif flow is not None and plan is not None and plan.intent is Intent.MIXED:
            answer = finalize_flow_without_claims(
                flow=flow,
                checklist=[],
                status=str(state.get("flow_status") or "unknown"),
                handoff=handoff,
                closed=closed,
                reason_message=_message_for(reason),
            )
            outcome_code = reason.value if reason else "UNVERIFIABLE"
        else:
            outcome_code = reason.value if reason else "UNVERIFIABLE"
            answer = finalize_abstain(
                message=_message_for(reason),
                handoff=handoff,
                missing_slots=missing_slots,
                warnings=extra_warnings,
                semantic_unchecked=semantic_unchecked,
            )

        trace = _append_trace(
            state,
            "finalize",
            started,
            outcome=outcome_code,
            citations=len(citations),
            checklist=len(checklist),
            handoff=handoff.as_dict(),
            semantic_unchecked=semantic_unchecked,
            coverage=state.get("coverage") or {},
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
        if plan is None:
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
    return "finalize"


def build_graph(nodes: GraphNodes, *, checkpointer: Any | None = None):
    graph = StateGraph(GraphState)
    graph.add_node("planner", nodes.planner)
    graph.add_node("flow", nodes.flow)
    graph.add_node("retriever", nodes.retriever)
    graph.add_node("advisor", nodes.advisor)
    graph.add_node("verifier", nodes.verifier)
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
        {"advisor": "advisor", "planner": "planner", "finalize": "finalize"},
    )
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
            "block_types": list(block_types or [BlockType.ARTICLE]),
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
            "retrieval_warnings": [],
            "scope_issues": [],
            "final_answer": "",
            "answer_markdown": "",
            "failed_claim_ids": [],
            "revise_issues": [],
            "revision_count": 0,
        }
        return self._graph.invoke(state, config)  # type: ignore[return-value]

    def reset(self, session_id: str) -> None:
        self._last_seen.pop(session_id, None)
        delete = getattr(self._checkpointer, "delete_thread", None)
        if callable(delete):
            delete(session_id)


__all__ = [
    "GRAPH_VERSION",
    "GraphNodes",
    "GraphRunner",
    "build_graph",
    "route_after_advisor",
    "route_after_flow",
    "route_after_planner",
    "route_after_retriever",
    "route_after_verifier",
]

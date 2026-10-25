"""LangGraph 会话状态契约。

W6 在原有骨架（规格书 5.7）上补齐图节点之间共享的字段。所有节点只通过
``GraphState`` 交互；原始隐私不写入 trace，checkpoint 只保存会话所需的最
小状态（slots/flow/claims/trace）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from campusguide.agents.plan import Plan
from campusguide.domain.enums import (
    Audience,
    GraphDecision,
    Intent,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import (
    ChecklistItem,
    Claim,
    Evidence,
    FlowDefinition,
    FlowInstance,
    PolicyFilters,
    SlotValue,
    VerificationIssue,
)
from campusguide.domain.profiles import UserProfile


class GraphState(TypedDict, total=False):
    # 既有骨架
    session_id: str
    turn_id: int
    user_profile: UserProfile | None
    messages: list[dict[str, str]]
    intent: Intent | None
    flow_id: str | None
    flow_instance: FlowInstance | None
    slots: dict[str, SlotValue]
    missing_slots: list[str]
    query_plan: list[dict[str, Any]]
    filters: PolicyFilters | None
    evidence: list[Evidence]
    claims: list[Claim]
    drafts: list[Any]
    draft: dict[str, Any] | None
    verification: dict[str, Any] | None
    verification_issues: list[dict[str, Any]]
    draft_records: list[dict[str, Any]]
    probe: dict[str, Any]
    probe_ms: int
    outcome: dict[str, Any] | None
    retry_count: int
    revision_count: int
    clarify_count: int
    trace: list[dict[str, Any]]

    # W6 图扩展
    question: str
    as_of: date | None
    audience: Audience | None
    block_types: list[Any]
    duration_ms: int
    started_at: str
    _started_perf: float
    plan: Plan | None
    flow_definition: FlowDefinition | None
    flow_evaluation: dict[str, Any] | None
    flow_status: str | None
    checklist: list[ChecklistItem]
    clarification: dict[str, Any] | None
    semantic: dict[str, Any] | None
    semantic_unchecked: bool
    handoff: dict[str, Any] | None
    final_answer: str
    answer_markdown: str
    citations: list[dict[str, Any]]
    dropped_claim_ids: list[str]
    failed_claim_ids: list[str]
    revise_issues: list[dict[str, Any]]
    history_claims: list[dict[str, Any]]
    decision: GraphDecision | None
    reason_code: OutcomeCode | None
    scope_issues: list[VerificationIssue]
    retrieval_warnings: list[str]
    coverage: dict[str, list[str]]
    questions: list[str]
    declined_slots: list[str]
    asked_slots: list[str]
    pending_slot: str | None
    pending_question: str
    pending_flow_ids: list[str]
    index_version: str
    semantic_mode: str
    planner_source: str
    answer_claim_ids: list[str]
    flow_only: bool
    closed: bool
    verification_decision: VerificationDecision | None

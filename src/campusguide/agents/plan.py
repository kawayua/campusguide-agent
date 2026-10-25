"""W6 Planner 输出契约。

Planner 只决定“问什么、走不走流程、需不需要澄清”，不生成政策结论。
所有字段都经过 Pydantic + 注册值校验；LLM 输出不合法时退回规则结果。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from campusguide.domain.enums import Intent, OutcomeCode
from campusguide.domain.models import SlotValue


class QueryPlanItem(BaseModel):
    query_id: str
    text: str
    route: Literal["policy", "flow"] = "policy"
    filters: dict = Field(default_factory=dict)
    covered_by: list[str] = Field(default_factory=list)


class Clarification(BaseModel):
    reason: Literal[
        "AUDIENCE_UNKNOWN",
        "VERSION_UNKNOWN",
        "FLOW_AMBIGUOUS",
        "SLOT_MISSING",
    ]
    question: str
    allowed_values: list[str] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    intent: Intent = Intent.POLICY_QA
    flow_id: str | None = None
    queries: list[QueryPlanItem] = Field(default_factory=list)
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification: Clarification | None = None
    planner_source: Literal["rule", "llm", "fallback"] = "rule"
    # guard 命中的直接结果；非空时图直接进 Finalize，不检索。
    outcome: OutcomeCode | None = None
    guard_message: str = ""
    # 歧义候选，供下一轮“1/2/3”选择。
    pending_flow_ids: list[str] = Field(default_factory=list)
    dropped_filters: list[str] = Field(default_factory=list)
    dropped_queries: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    flow_status: str | None = None
    declined_slots: list[str] = Field(default_factory=list)
    asked_slots: list[str] = Field(default_factory=list)
    pending_slot: str | None = None

    @property
    def is_guard(self) -> bool:
        return self.outcome is not None

    def trace_payload(self) -> dict:
        return {
            "intent": self.intent.value,
            "flow_id": self.flow_id,
            "queries": [item.text for item in self.queries],
            "missing_slots": list(self.missing_slots),
            "needs_clarification": self.needs_clarification,
            "clarification": (
                self.clarification.model_dump(mode="json") if self.clarification else None
            ),
            "planner_source": self.planner_source,
            "outcome": self.outcome.value if self.outcome else None,
            "pending_flow_ids": list(self.pending_flow_ids),
            "dropped_filters": list(self.dropped_filters),
            "dropped_queries": list(self.dropped_queries),
            "warnings": list(self.warnings),
            "flow_status": self.flow_status,
        }

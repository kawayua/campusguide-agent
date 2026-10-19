"""LangGraph 会话状态契约（规格书 5.7）。"""

from __future__ import annotations

from typing import Any, TypedDict

from campusguide.domain.enums import Intent
from campusguide.domain.models import (
    Claim,
    Evidence,
    FlowInstance,
    PolicyFilters,
    SlotValue,
)


class GraphState(TypedDict, total=False):
    session_id: str
    turn_id: int
    user_profile: dict[str, Any]
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
    draft: dict[str, Any] | None
    verification: dict[str, Any] | None
    outcome: dict[str, Any] | None
    retry_count: int
    revision_count: int
    clarify_count: int
    trace: list[dict[str, Any]]

"""流程目录：本地 registry（离线/测试）与 policy-meta MCP（生产）。

Planner/FlowExecutor 只依赖本模块的 ``FlowCatalog`` 协议，不直接读 YAML。
生产路径通过 ``get_flow`` 获取完整 DSL，本地 registry 仅用于离线测试/降级。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Protocol

from campusguide.domain.enums import Audience
from campusguide.domain.models import CalendarEvent, FlowDefinition, SlotValue
from campusguide.flows.engine import FlowEvaluation, evaluate_flow
from campusguide.flows.registry import FlowRegistry


class FlowCatalog(Protocol):
    def list_flows(
        self,
        *,
        as_of: date,
        audience: Audience | None = None,
    ) -> list[FlowDefinition]: ...

    def get_flow(self, flow_id: str, *, as_of: date) -> FlowDefinition | None: ...

    def calendar(self) -> dict[str, CalendarEvent]: ...

    def evaluate(
        self,
        flow: FlowDefinition,
        slots: Mapping[str, SlotValue | object] | None,
        *,
        as_of: date,
        done_step_ids: set[str] | None = None,
        declined_slots: set[str] | None = None,
        asked_slots: set[str] | None = None,
    ) -> FlowEvaluation: ...


class LocalFlowCatalog:
    def __init__(
        self,
        registry: FlowRegistry,
        *,
        calendar: Mapping[str, CalendarEvent] | None = None,
    ) -> None:
        self._registry = registry
        self._calendar = dict(calendar or {})

    @property
    def registry(self) -> FlowRegistry:
        return self._registry

    def list_flows(
        self,
        *,
        as_of: date,
        audience: Audience | None = None,
    ) -> list[FlowDefinition]:
        return self._registry.active_flows(as_of=as_of, audience=audience)

    def get_flow(self, flow_id: str, *, as_of: date) -> FlowDefinition | None:
        return self._registry.get_flow(flow_id, as_of=as_of)

    def calendar(self) -> dict[str, CalendarEvent]:
        return dict(self._calendar)

    def evaluate(
        self,
        flow: FlowDefinition,
        slots: Mapping[str, SlotValue | object] | None,
        *,
        as_of: date,
        done_step_ids: set[str] | None = None,
        declined_slots: set[str] | None = None,
        asked_slots: set[str] | None = None,
    ) -> FlowEvaluation:
        return evaluate_flow(
            flow,
            slots,
            as_of=as_of,
            calendar=self._calendar,
            done_step_ids=done_step_ids,
            declined_slots=declined_slots,
            asked_slots=asked_slots,
        )


class MCPFlowCatalog:
    """通过 policy-meta 读取流程；按 as_of 缓存完整 DSL。"""

    def __init__(self, meta_client: object) -> None:
        self._meta = meta_client
        self._cache: dict[date, dict[str, FlowDefinition]] = {}
        self._calendar: dict[str, CalendarEvent] | None = None

    def _load(self, as_of: date) -> dict[str, FlowDefinition]:
        if as_of in self._cache:
            return self._cache[as_of]
        payload = self._meta.list_flows(as_of=as_of.isoformat())  # type: ignore[attr-defined]
        flows: dict[str, FlowDefinition] = {}
        for summary in payload.get("flows") or []:
            flow_id = summary.get("flow_id") if isinstance(summary, dict) else None
            if not flow_id:
                continue
            detail = self._meta.get_flow(flow_id, as_of=as_of.isoformat())  # type: ignore[attr-defined]
            flows[flow_id] = FlowDefinition.model_validate(detail)
        self._cache[as_of] = flows
        return flows

    def list_flows(
        self,
        *,
        as_of: date,
        audience: Audience | None = None,
    ) -> list[FlowDefinition]:
        flows = list(self._load(as_of).values())
        if audience is not None:
            flows = [
                flow
                for flow in flows
                if Audience.ALL in set(flow.audience) or audience in set(flow.audience)
            ]
        return sorted(flows, key=lambda item: item.flow_id)

    def get_flow(self, flow_id: str, *, as_of: date) -> FlowDefinition | None:
        try:
            return self._load(as_of).get(flow_id)
        except Exception:
            # 单个流程 get_flow 失败时允许 Planner 回退 policy_qa。
            return None

    def calendar(self) -> dict[str, CalendarEvent]:
        if self._calendar is not None:
            return dict(self._calendar)
        payload = self._meta.get_calendar()  # type: ignore[attr-defined]
        events = {
            item["event_id"]: CalendarEvent.model_validate(item)
            for item in payload.get("events") or []
            if isinstance(item, dict) and item.get("event_id")
        }
        self._calendar = events
        return dict(events)

    def evaluate(
        self,
        flow: FlowDefinition,
        slots: Mapping[str, SlotValue | object] | None,
        *,
        as_of: date,
        done_step_ids: set[str] | None = None,
        declined_slots: set[str] | None = None,
        asked_slots: set[str] | None = None,
    ) -> FlowEvaluation:
        return evaluate_flow(
            flow,
            slots,
            as_of=as_of,
            calendar=self.calendar(),
            done_step_ids=done_step_ids,
            declined_slots=declined_slots,
            asked_slots=asked_slots,
        )

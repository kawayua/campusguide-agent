"""流程 DAG 求值与 checklist 渲染。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from campusguide.domain.models import (
    CalendarEvent,
    ChecklistItem,
    FlowDefinition,
    FlowStep,
    SlotDefinition,
    SlotValue,
)
from campusguide.flows.predicates import (
    PredicateError,
    Truth,
    evaluate_predicate,
    slot_names,
    validate_predicate,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class FlowEvaluation:
    flow: FlowDefinition
    status: str
    checklist: list[ChecklistItem] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    next_slot: SlotDefinition | None = None
    eligible: bool | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.status == "closed"


def _as_datetime(value: date | datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    return datetime.combine(value, time(23, 59, 59, 999999), tzinfo=SHANGHAI)


def _parse_deadline(
    raw: str,
    calendar: Mapping[str, CalendarEvent] | None,
) -> datetime | None:
    text = raw.strip()
    if text.startswith("calendar:"):
        event_id = text.split(":", 1)[1]
        event = (calendar or {}).get(event_id)
        if event is None:
            return None
        value = event.end or event.start
        return datetime.combine(value, time(23, 59, 59), tzinfo=SHANGHAI)
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(23, 59, 59), tzinfo=SHANGHAI)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)
    except ValueError:
        return None


def _closed_note(
    step: FlowStep, as_of: datetime, calendar: Mapping[str, CalendarEvent] | None
) -> str | None:
    if step.deadline:
        deadline = _parse_deadline(step.deadline, calendar)
        if deadline is not None and as_of > deadline:
            return f"本批次截止：{deadline.strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai）"
        return None
    if step.closed_observed_at and as_of.date() >= step.closed_observed_at:
        return (
            f"公开信息显示本批次已结束（公示日期 {step.closed_observed_at.isoformat()}）；"
            "具体截止日未公开，以学院当年通知为准"
        )
    return None


def _evaluate_when(
    expression: str | None,
    slots: Mapping[str, Any],
    *,
    today: date,
    slot_map: Mapping[str, SlotDefinition],
) -> Truth:
    if not expression:
        return Truth.TRUE
    ast = validate_predicate(expression, slot_map)
    return evaluate_predicate(ast, slots, today=today)


def _topological_order(flow: FlowDefinition) -> list[FlowStep]:
    by_id = {step.id: step for step in flow.steps}
    ordered: list[FlowStep] = []
    resolved: set[str] = set()
    remaining = list(flow.steps)
    while remaining:
        progressed = False
        for step in list(remaining):
            if all(dep in resolved for dep in step.depends_on):
                ordered.append(step)
                resolved.add(step.id)
                remaining.remove(step)
                progressed = True
        if not progressed:
            raise ValueError(f"流程 DAG 存在环：{flow.flow_id}")
    # by_id 只用于保证依赖 ID 已在上游校验过。
    assert len(by_id) == len(ordered)
    return ordered


def _step_labels(flow: FlowDefinition) -> dict[str, str]:
    return {step.id: step.title for step in flow.steps}


def evaluate_flow(
    flow: FlowDefinition,
    slots: Mapping[str, SlotValue | Any] | None = None,
    *,
    as_of: date | datetime | None = None,
    calendar: Mapping[str, CalendarEvent] | None = None,
    done_step_ids: set[str] | None = None,
    declined_slots: set[str] | None = None,
    asked_slots: set[str] | None = None,
) -> FlowEvaluation:
    slots = slots or {}
    as_of_dt = _as_datetime(as_of)
    today = as_of_dt.date()
    slot_map = {slot.name: slot for slot in flow.slots}
    done_step_ids = done_step_ids or set()
    declined_slots = declined_slots or set()
    asked_slots = asked_slots or set()
    warnings: list[str] = []

    # 前置条件先算；false 时所有步骤 blocked；unknown 按 on_unknown 处理。
    prerequisite_failure: str | None = None
    prerequisite_unknown: str | None = None
    unknown_prerequisite_slots: set[str] = set()
    for prerequisite in flow.prerequisites:
        try:
            value = _evaluate_when(prerequisite.when, slots, today=today, slot_map=slot_map)
        except PredicateError as exc:
            warnings.append(f"前置条件 {prerequisite.id} 求值失败：{exc}")
            continue
        if value is Truth.FALSE:
            prerequisite_failure = prerequisite.description
            break
        if value is Truth.UNKNOWN:
            unknown_prerequisite_slots.update(
                slot_names(validate_predicate(prerequisite.when, slot_map))
            )
            if prerequisite.on_unknown == "block":
                prerequisite_unknown = prerequisite.description

    steps = _topological_order(flow)
    labels = _step_labels(flow)
    statuses: dict[str, str] = {}
    items: list[ChecklistItem] = []
    missing: set[str] = set()

    for step in steps:
        when_value = Truth.TRUE
        if step.when:
            try:
                when_value = _evaluate_when(step.when, slots, today=today, slot_map=slot_map)
            except PredicateError as exc:
                warnings.append(f"步骤 {step.id} 谓词求值失败：{exc}")
                when_value = Truth.UNKNOWN

        if when_value is Truth.FALSE:
            status = "not_applicable"
            status_note = "当前条件不适用"
            blocked_reason = None
        elif prerequisite_failure:
            status = "blocked"
            status_note = None
            blocked_reason = prerequisite_failure
        elif prerequisite_unknown:
            status = "blocked"
            status_note = None
            blocked_reason = prerequisite_unknown
        else:
            closed_note = _closed_note(step, as_of_dt, calendar)
            if closed_note:
                status = "closed"
                status_note = closed_note
                blocked_reason = None
            elif when_value is Truth.UNKNOWN:
                missing.update(slot_names(validate_predicate(step.when, slot_map)))
                if step.on_unknown == "block":
                    status = "blocked"
                    blocked_reason = "缺少判断条件所需信息"
                elif step.on_unknown == "conditional":
                    status = "conditional"
                    blocked_reason = None
                else:
                    status = "conditional"
                    blocked_reason = None
                status_note = "需要补充信息后才能判断"
            else:
                unresolved = [
                    dep
                    for dep in step.depends_on
                    if dep not in done_step_ids and statuses.get(dep) != "done"
                ]
                if unresolved:
                    status = "blocked"
                    blocked_reason = "先完成：" + "、".join(
                        labels.get(dep, dep) for dep in unresolved
                    )
                    status_note = None
                elif step.id in done_step_ids:
                    status = "done"
                    blocked_reason = None
                    status_note = None
                else:
                    status = "available"
                    blocked_reason = None
                    status_note = None

        statuses[step.id] = status
        items.append(
            ChecklistItem(
                step_id=step.id,
                title=step.title,
                status=status,
                description=step.description,
                materials=list(step.materials),
                deadline=step.deadline,
                deadline_note=step.deadline_note,
                department=step.responsible_department,
                citations=list(step.citations),
                blocked_reason=blocked_reason,
                status_note=status_note,
            )
        )

    # 只对未关闭/未失效的路径收集未知槽位。
    missing.update(unknown_prerequisite_slots)
    active_is_closed_only = all(
        statuses.get(step.id) == "closed"
        for step in steps
        if statuses.get(step.id) != "not_applicable"
    ) and any(statuses.get(step.id) == "closed" for step in steps)
    if active_is_closed_only or prerequisite_failure:
        missing.clear()

    missing = {
        name
        for name in missing
        if name in slot_map and name not in slots and name not in declined_slots
    }
    ordered_missing = sorted(missing, key=lambda name: (slot_map[name].priority, name))
    next_slot = None
    for name in ordered_missing:
        if name not in asked_slots:
            next_slot = slot_map[name]
            break
    if next_slot is None and ordered_missing:
        next_slot = slot_map[ordered_missing[0]]

    dated = [
        step
        for step in steps
        if (step.deadline or step.closed_observed_at) and statuses.get(step.id) != "not_applicable"
    ]
    if not dated:
        flow_status = "unknown"
    elif all(statuses.get(step.id) == "closed" for step in dated):
        flow_status = "closed"
    elif any(statuses.get(step.id) == "closed" for step in dated):
        flow_status = "partial"
    else:
        flow_status = "open"

    return FlowEvaluation(
        flow=flow,
        status=flow_status,
        checklist=items,
        missing_slots=ordered_missing,
        next_slot=next_slot,
        eligible=False if prerequisite_failure else (None if prerequisite_unknown else True),
        warnings=warnings,
    )


_STATUS_LABELS = {
    "available": "可办理",
    "blocked": "受阻",
    "conditional": "待确认",
    "done": "已完成",
    "not_applicable": "不适用",
    "closed": "已截止",
}

_FLOW_STATUS_LABELS = {
    "open": "可办理",
    "partial": "部分截止",
    "closed": "已截止",
    "unknown": "以官方通知为准",
}


def render_checklist_text(
    evaluation: FlowEvaluation,
    *,
    include_not_applicable: bool = False,
) -> str:
    status_label = _FLOW_STATUS_LABELS.get(evaluation.status, evaluation.status)
    lines = [f"{evaluation.flow.title}（状态：{status_label}）"]
    if evaluation.flow.batch_label:
        lines.append(f"批次：{evaluation.flow.batch_label}")
    for item in evaluation.checklist:
        if item.status == "not_applicable" and not include_not_applicable:
            continue
        lines.append(f"- [{_STATUS_LABELS[item.status]}] {item.title}")
        if item.description:
            lines.append(f"  {item.description}")
        if item.materials:
            lines.append("  材料：" + "、".join(item.materials))
        if item.deadline:
            lines.append(f"  截止：{item.deadline}")
        if item.deadline_note:
            lines.append(f"  时限说明：{item.deadline_note}")
        if item.status_note:
            lines.append(f"  状态说明：{item.status_note}")
        if item.blocked_reason:
            lines.append(f"  原因：{item.blocked_reason}")
        lines.append(f"  部门：{item.department}")
        if item.citations:
            lines.append("  依据：" + "、".join(item.citations))
    return "\n".join(lines)

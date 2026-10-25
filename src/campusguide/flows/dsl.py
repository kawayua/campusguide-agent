"""流程 YAML 加载与 schema 校验。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from campusguide.domain.models import FlowDefinition
from campusguide.flows.predicates import PredicateError, validate_predicate


class FlowLoadError(ValueError):
    """流程 YAML 不合法。"""


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_deadline(value: str) -> date | datetime:
    raw = value.strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw)
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FlowLoadError(f"deadline 不是合法 ISO 日期/时间：{value!r}") from exc


def _validate_dag(steps: list[Any]) -> None:
    ids = [step.id for step in steps]
    if len(ids) != len(set(ids)):
        raise FlowLoadError("步骤 ID 不能重复")
    known = set(ids)
    for step in steps:
        if step.id in step.depends_on:
            raise FlowLoadError(f"步骤不能依赖自身：{step.id}")
        missing = [item for item in step.depends_on if item not in known]
        if missing:
            raise FlowLoadError(f"步骤 {step.id} 依赖不存在：{missing}")

    remaining = {step.id: set(step.depends_on) for step in steps}
    resolved: set[str] = set()
    while remaining:
        ready = {step_id for step_id, deps in remaining.items() if deps <= resolved}
        if not ready:
            raise FlowLoadError(f"流程步骤存在环：{sorted(remaining)}")
        for step_id in ready:
            resolved.add(step_id)
            remaining.pop(step_id)


def _validate_navigation_only(flow: FlowDefinition) -> None:
    if not flow.navigation_only:
        return
    if not flow.steps:
        raise FlowLoadError(f"{flow.flow_id} 标注 navigation_only 但没有步骤")
    for step in flow.steps:
        if not step.citation_exempt:
            raise FlowLoadError(f"navigation_only 流程的步骤必须 citation_exempt：{step.id}")
        if step.materials or step.deadline or step.closed_observed_at:
            raise FlowLoadError(f"navigation_only 步骤不能携带材料/截止事实：{step.id}")


def validate_flow(
    flow: FlowDefinition,
    *,
    valid_citations: Iterable[str] | None = None,
    calendar_event_ids: Iterable[str] | None = None,
    constants: dict[str, str] | None = None,
) -> FlowDefinition:
    if not _SLUG_RE.match(flow.flow_id):
        raise FlowLoadError(f"flow_id 必须是小写 slug：{flow.flow_id!r}")
    if not flow.version.strip():
        raise FlowLoadError(f"{flow.flow_id} 缺少 version")
    if not flow.steps:
        raise FlowLoadError(f"{flow.flow_id} 缺少 steps")
    if not flow.slots and any(step.when for step in flow.steps):
        raise FlowLoadError(f"{flow.flow_id} 有 when 但没有定义 slots")

    slot_names = [slot.name for slot in flow.slots]
    if len(slot_names) != len(set(slot_names)):
        raise FlowLoadError(f"{flow.flow_id} 槽位名称重复")
    slot_map = {slot.name: slot for slot in flow.slots}

    prereq_ids = [item.id for item in flow.prerequisites]
    if len(prereq_ids) != len(set(prereq_ids)):
        raise FlowLoadError(f"{flow.flow_id} 前置条件 ID 重复")

    _validate_dag(flow.steps)
    _validate_navigation_only(flow)

    valid_set = set(valid_citations) if valid_citations is not None else None
    calendar_set = set(calendar_event_ids) if calendar_event_ids is not None else None

    def check_citations(label: str, citations: list[str], exempt: bool = False) -> None:
        if not citations and not exempt:
            raise FlowLoadError(f"{flow.flow_id} 的 {label} 缺少 citations")
        if valid_set is not None:
            missing = [item for item in citations if item not in valid_set]
            if missing:
                raise FlowLoadError(f"{flow.flow_id} 的 {label} 引用不存在：{missing}")

    for prerequisite in flow.prerequisites:
        if not prerequisite.when.strip():
            raise FlowLoadError(f"{flow.flow_id} 前置条件 {prerequisite.id} 缺少 when")
        try:
            validate_predicate(prerequisite.when, slot_map, constants)
        except PredicateError as exc:
            raise FlowLoadError(
                f"{flow.flow_id} 前置条件 {prerequisite.id} 谓词非法：{exc}"
            ) from exc
        check_citations(f"前置条件 {prerequisite.id}", prerequisite.citations)

    for step in flow.steps:
        if step.when:
            try:
                validate_predicate(step.when, slot_map, constants)
            except PredicateError as exc:
                raise FlowLoadError(f"{flow.flow_id} 步骤 {step.id} 谓词非法：{exc}") from exc
        check_citations(f"步骤 {step.id}", step.citations, step.citation_exempt)
        if step.deadline:
            raw = step.deadline.strip()
            if raw.startswith("calendar:"):
                event_id = raw.split(":", 1)[1]
                if not event_id:
                    raise FlowLoadError(f"{flow.flow_id} 步骤 {step.id} 的 calendar 引用为空")
                if calendar_set is not None and event_id not in calendar_set:
                    raise FlowLoadError(
                        f"{flow.flow_id} 步骤 {step.id} 的 calendar 事件不存在：{event_id}"
                    )
            else:
                _parse_deadline(raw)

    for slot in flow.slots:
        if slot.type == "integer":
            minimum = (slot.validation or {}).get("min")
            maximum = (slot.validation or {}).get("max")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise FlowLoadError(f"{flow.flow_id} 槽位 {slot.name} 的 min > max")
    return flow


def load_flow(
    path: Path,
    *,
    valid_citations: Iterable[str] | None = None,
    calendar_event_ids: Iterable[str] | None = None,
    constants: dict[str, str] | None = None,
) -> FlowDefinition:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FlowLoadError(f"{path} 不是合法 YAML：{exc}") from exc
    if not isinstance(payload, dict):
        raise FlowLoadError(f"{path} 顶层必须是对象")
    try:
        flow = FlowDefinition.model_validate(payload)
    except Exception as exc:
        raise FlowLoadError(f"{path} 不符合 FlowDefinition：{exc}") from exc
    return validate_flow(
        flow,
        valid_citations=valid_citations,
        calendar_event_ids=calendar_event_ids,
        constants=constants,
    )


def _overlaps(left: FlowDefinition, right: FlowDefinition) -> bool:
    left_end = left.effective_to or date.max
    right_end = right.effective_to or date.max
    return left.effective_from <= right_end and right.effective_from <= left_end


def load_flows(
    directory: Path,
    *,
    valid_citations: Iterable[str] | None = None,
    calendar_event_ids: Iterable[str] | None = None,
    constants: dict[str, str] | None = None,
) -> list[FlowDefinition]:
    if not directory.exists():
        raise FlowLoadError(f"流程目录不存在：{directory}")
    flows: list[FlowDefinition] = []
    for path in sorted(directory.glob("*.yaml")):
        flows.append(
            load_flow(
                path,
                valid_citations=valid_citations,
                calendar_event_ids=calendar_event_ids,
                constants=constants,
            )
        )
    seen: set[tuple[str, str]] = set()
    for flow in flows:
        key = (flow.flow_id, flow.version)
        if key in seen:
            raise FlowLoadError(f"重复流程版本：{flow.flow_id}@{flow.version}")
        seen.add(key)
    for index, left in enumerate(flows):
        for right in flows[index + 1 :]:
            if left.flow_id == right.flow_id and _overlaps(left, right):
                raise FlowLoadError(
                    f"流程版本重叠：{left.flow_id} {left.version} 与 {right.version}"
                )
    return sorted(flows, key=lambda item: (item.flow_id, item.effective_from))

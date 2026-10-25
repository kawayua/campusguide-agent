from __future__ import annotations

from datetime import date

from campusguide.domain.enums import Audience
from campusguide.domain.models import (
    CalendarEvent,
    FlowDefinition,
    FlowStep,
    HandoffInfo,
    Prerequisite,
    SlotDefinition,
)
from campusguide.flows.engine import evaluate_flow, render_checklist_text


def slot(name: str, required: bool = True, priority: int = 10, type_: str = "string"):
    return SlotDefinition(
        name=name,
        type=type_,
        required=required,
        question=f"{name}?",
        why_needed="test",
        priority=priority,
        allowed_values=["info", "agri"] if type_ == "enum" else None,
    )


def flow(steps: list[FlowStep], *, slots=None, prerequisites=None) -> FlowDefinition:
    return FlowDefinition(
        flow_id="demo-flow",
        version="v2026",
        title="示例流程",
        description="d",
        audience=[Audience.UNDERGRADUATE],
        effective_from=date(2026, 9, 1),
        batch_label="2026 批次",
        slots=slots or [slot("grade", type_="integer"), slot("college", type_="enum")],
        prerequisites=prerequisites or [],
        steps=steps,
        handoff=HandoffInfo(department="学生工作部"),
    )


def step(step_id: str, **kwargs) -> FlowStep:
    payload = {
        "id": step_id,
        "title": step_id,
        "description": "d",
        "responsible_department": "学生工作部",
        "citations": ["doc@v1#a"],
        "deadline_note": "以通知为准",
    }
    payload.update(kwargs)
    return FlowStep(**payload)


def test_closed_takes_precedence_and_skips_slots() -> None:
    f = flow([step("apply", deadline="2026-10-10T09:00:00+08:00")])
    ev = evaluate_flow(f, {}, as_of=date(2026, 10, 25))
    assert ev.status == "closed"
    assert ev.checklist[0].status == "closed"
    assert "本批次截止" in (ev.checklist[0].status_note or "")
    assert ev.missing_slots == []
    assert ev.next_slot is None


def test_open_on_earlier_date_and_asks_slot() -> None:
    f = flow([step("apply", deadline="2026-10-10T09:00:00+08:00", when="slots.grade >= 2")])
    ev = evaluate_flow(f, {}, as_of=date(2026, 9, 30))
    assert ev.status == "open"
    assert ev.checklist[0].status == "conditional"
    assert ev.missing_slots == ["grade"]
    assert ev.next_slot.name == "grade"


def test_partial_flow_marks_only_rising_deadline_open() -> None:
    f = flow(
        [
            step("merit", deadline="2026-10-10T09:00:00+08:00"),
            step("inspirational", deadline="2026-10-21"),
        ]
    )
    ev = evaluate_flow(f, {}, as_of=date(2026, 10, 18))
    assert ev.status == "partial"
    assert [item.status for item in ev.checklist] == ["closed", "available"]


def test_observed_closed_without_deadline() -> None:
    f = flow([step("grant", closed_observed_at=date(2026, 10, 21))])
    ev = evaluate_flow(f, {}, as_of=date(2026, 10, 25))
    assert ev.status == "closed"
    note = ev.checklist[0].status_note or ""
    assert "公示日期 2026-10-21" in note
    assert "具体截止日未公开" in note


def test_dependency_blocks_and_done_unblocks() -> None:
    f = flow([step("one"), step("two", depends_on=["one"])])
    ev = evaluate_flow(f, {}, as_of=date(2026, 9, 30))
    assert ev.checklist[0].status == "available"
    assert ev.checklist[1].status == "blocked"
    assert "先完成" in (ev.checklist[1].blocked_reason or "")
    ev = evaluate_flow(f, {}, as_of=date(2026, 9, 30), done_step_ids={"one"})
    assert ev.checklist[1].status == "available"


def test_when_false_is_not_applicable() -> None:
    f = flow([step("grad", when='slots.college == "agri"')])
    ev = evaluate_flow(f, {"college": "info"}, as_of=date(2026, 9, 30))
    assert ev.checklist[0].status == "not_applicable"
    assert ev.status == "unknown"


def test_prerequisite_false_blocks_all() -> None:
    f = flow(
        [step("apply", deadline="2026-10-10")],
        prerequisites=[
            Prerequisite(
                id="p1",
                description="必须是在校生",
                when='slots.college == "info"',
                on_false="ineligible",
                citations=["doc@v1#a"],
            )
        ],
    )
    ev = evaluate_flow(f, {"college": "agri"}, as_of=date(2026, 9, 30))
    assert ev.eligible is False
    assert ev.checklist[0].status == "blocked"
    assert ev.checklist[0].blocked_reason == "必须是在校生"


def test_date_only_deadline_is_end_of_day() -> None:
    f = flow([step("apply", deadline="2026-10-10")])
    assert evaluate_flow(f, {}, as_of=date(2026, 10, 9)).checklist[0].status == "available"
    assert evaluate_flow(f, {}, as_of=date(2026, 10, 10)).checklist[0].status == "closed"


def test_calendar_reference() -> None:
    f = flow([step("apply", deadline="calendar:apply-window")])
    event = CalendarEvent(
        event_id="apply-window",
        title="申请窗口",
        start=date(2026, 9, 20),
        end=date(2026, 10, 10),
        audience=[Audience.UNDERGRADUATE],
        academic_year="2026-2027",
    )
    assert (
        evaluate_flow(f, {}, as_of=date(2026, 10, 9), calendar={"apply-window": event}).status
        == "open"
    )
    assert (
        evaluate_flow(f, {}, as_of=date(2026, 10, 11), calendar={"apply-window": event}).status
        == "closed"
    )


def test_render_checklist_text_contains_status_and_citation() -> None:
    f = flow([step("apply", deadline="2026-10-10")])
    text = render_checklist_text(evaluate_flow(f, {}, as_of=date(2026, 10, 25)))
    assert "已截止" in text
    assert "doc@v1#a" in text

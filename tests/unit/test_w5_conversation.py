from __future__ import annotations

from datetime import date

from campusguide.conversation.service import ConversationService
from campusguide.domain.enums import Audience
from campusguide.domain.models import (
    FlowDefinition,
    FlowStep,
    HandoffInfo,
    Prerequisite,
    SlotDefinition,
)
from campusguide.flows.registry import FlowRegistry


def slot(name, type_, *, allowed=None, aliases=None, required=True, priority=10):
    return SlotDefinition(
        name=name,
        type=type_,
        required=required,
        question=f"请提供{name}",
        why_needed="判断资格",
        allowed_values=allowed,
        aliases=aliases,
        priority=priority,
        validation={"min": 1, "max": 4} if type_ == "integer" else None,
    )


def scholarship_flow() -> FlowDefinition:
    return FlowDefinition(
        flow_id="scholarship-application",
        version="v2026",
        title="本科生国家奖学金申请",
        description="国家级奖学金",
        audience=[Audience.UNDERGRADUATE],
        aliases=["国家奖学金", "奖学金"],
        effective_from=date(2026, 9, 1),
        batch_label="2026 批次",
        slots=[
            slot(
                "college",
                "enum",
                allowed=["info", "agri"],
                aliases={"info": ["数信", "数学与信息学院"], "agri": ["农学院"]},
                priority=10,
            ),
            slot(
                "award_type",
                "enum",
                allowed=["merit", "aid"],
                aliases={"merit": ["国家奖学金", "国奖"], "aid": ["国家励志奖学金", "励志"]},
                priority=20,
            ),
            slot("grade", "integer", priority=30),
            slot("has_record", "boolean", priority=40),
        ],
        prerequisites=[
            Prerequisite(
                id="eligibility",
                description="需为二年级及以上、无处分且属于目标学院",
                when='slots.college in ["info", "agri"] and slots.grade >= 2 and slots.has_record == false',
                on_false="ineligible",
                on_unknown="ask",
                citations=["doc@v1#a"],
            )
        ],
        steps=[
            FlowStep(
                id="merit",
                title="提交国奖申请",
                description="提交申请表",
                when='slots.award_type == "merit"',
                materials=["申请表"],
                deadline="2026-10-10T09:00:00+08:00",
                responsible_department="学院学工办",
                citations=["doc@v1#a"],
            ),
            FlowStep(
                id="aid",
                title="提交励志申请",
                description="提交申请表",
                when='slots.award_type == "aid"',
                materials=["申请表"],
                deadline="2026-10-21",
                responsible_department="学院学工办",
                citations=["doc@v1#a"],
            ),
            FlowStep(
                id="prepare",
                title="提前准备材料",
                description="准备成绩单",
                deadline_note="可提前准备",
                responsible_department="学院学工办",
                citations=["doc@v1#a"],
            ),
        ],
        handoff=HandoffInfo(department="学生工作部", note="以当年通知为准"),
    )


def service(as_of: date, flows=None) -> ConversationService:
    return ConversationService(
        FlowRegistry(flows or [scholarship_flow()]),
        today_provider=lambda: as_of,
    )


def test_closed_reply_does_not_ask_slots() -> None:
    svc = service(date(2026, 10, 25))
    reply = svc.handle("s1", "我想申请国家奖学金")
    assert reply.closed is True
    assert reply.flow_id == "scholarship-application"
    assert reply.next_slot is None
    assert "已截止" in reply.text
    assert "请提供" not in reply.text


def test_open_flow_asks_and_fills_slots_then_renders_checklist() -> None:
    svc = service(date(2026, 9, 30))
    first = svc.handle("s1", "我想申请国家奖学金")
    assert first.closed is False
    assert first.next_slot == "college"
    second = svc.handle("s1", "数信，大二，没受过处分")
    assert second.flow_id == "scholarship-application"
    assert second.status == "open"
    assert second.next_slot is None
    assert "提交国奖申请" in second.text
    assert "已截止" not in second.text


def test_partial_flow_respects_award_type() -> None:
    svc = service(date(2026, 10, 18))
    reply = svc.handle("s1", "我想申请国家奖学金")
    assert reply.closed is True

    svc2 = service(date(2026, 10, 18))
    reply = svc2.handle("s1", "我想申请奖学金")
    assert reply.closed is False
    assert reply.status == "partial"
    fill = svc2.handle("s1", "数信，大二，没受过处分，国家励志奖学金")
    assert fill.status == "open"
    assert fill.next_slot is None
    assert "提交励志申请" in fill.text


def test_correction_recomputes_without_reasking() -> None:
    svc = service(date(2026, 9, 30))
    svc.handle("s1", "我想申请国家奖学金")
    svc.handle("s1", "数信，大二，没受过处分")
    reply = svc.handle("s1", "其实我是大三")
    assert reply.next_slot is None
    assert "提交国奖申请" in reply.text
    assert svc.store.get("s1").slots["grade"].value == 3


def test_declined_slot_not_reasked() -> None:
    svc = service(date(2026, 9, 30))
    first = svc.handle("s1", "我想申请国家奖学金")
    assert first.next_slot == "college"
    second = svc.handle("s1", "跳过")
    assert second.next_slot != "college"
    assert "college" in svc.store.get("s1").declined_slots


def test_ambiguous_flow_selection() -> None:
    other = scholarship_flow().model_copy(
        update={"flow_id": "other-flow", "title": "另一项奖学金", "aliases": ["奖学金"]}
    )
    svc = service(date(2026, 9, 30), flows=[scholarship_flow(), other])
    reply = svc.handle("s1", "我想申请奖学金")
    assert len(reply.ambiguous_flow_ids) == 2
    chosen = svc.handle("s1", "1")
    assert chosen.flow_id in {"scholarship-application", "other-flow"}
    assert chosen.ambiguous_flow_ids == []


def test_fallback_when_no_flow_matches() -> None:
    svc = service(date(2026, 9, 30))
    reply = svc.handle("s1", "今天天气不错")
    assert reply.flow_id is None
    assert "没有匹配" in reply.text

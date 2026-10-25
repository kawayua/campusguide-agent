from __future__ import annotations

from datetime import date
from pathlib import Path

from campusguide.conversation.service import ConversationService
from campusguide.flows.calendar import load_calendar
from campusguide.flows.registry import FlowRegistry

ROOT = Path(__file__).resolve().parents[2]


def make_service(as_of: date) -> ConversationService:
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows")
    calendar = {
        event.event_id: event for event in load_calendar(ROOT / "configs" / "calendar.yaml")
    }
    return ConversationService(
        registry,
        calendar=calendar,
        today_provider=lambda: as_of,
    )


def test_scholarship_is_closed_on_2026_10_25() -> None:
    service = make_service(date(2026, 10, 25))
    reply = service.handle("s", "我想申请国家奖学金")
    assert reply.closed is True
    assert reply.flow_id == "scholarship-application"
    assert "2026-10-10" in reply.text
    assert reply.next_slot is None


def test_scholarship_open_and_fill_on_2026_09_30() -> None:
    service = make_service(date(2026, 9, 30))
    first = service.handle("s", "我想申请国家奖学金")
    assert first.next_slot == "college"
    second = service.handle("s", "数信，大二，没受过处分")
    assert second.status == "open"
    assert second.next_slot is None
    assert "数信学院：国家奖学金申请" in second.text
    assert "2026-10-10T09:00:00+08:00" in second.text


def test_scholarship_partial_before_award_choice() -> None:
    service = make_service(date(2026, 10, 18))
    reply = service.handle("s", "我想申请奖学金")
    assert reply.status == "partial"
    assert reply.closed is False
    filled = service.handle("s", "数信，大二，没受过处分，国家励志奖学金，已认定困难")
    assert filled.status == "open"
    assert "国家励志奖学金申请" in filled.text


def test_agri_observed_closed_and_unknown_merit() -> None:
    service = make_service(date(2026, 10, 25))
    reply = service.handle("s1", "我是农学院，想申请国家励志奖学金")
    assert reply.closed is True
    assert "具体截止日未公开" in reply.text

    merit = make_service(date(2026, 10, 25)).handle("s2", "我是农学院，想申请国家奖学金")
    assert merit.closed is False
    assert merit.next_slot in {"grade", "has_record"}
    filled = make_service(date(2026, 10, 25))
    filled.handle("s3", "我是农学院，想申请国家奖学金")
    result = filled.handle("s3", "大二，没受过处分")
    assert "未检索到农学院 2026 年本科生国家奖学金公开通知" in result.text
    assert result.status == "unknown"


def test_certificate_undergraduate_self_print() -> None:
    service = make_service(date(2026, 10, 25))
    assert service.handle("s", "我要开在读证明").next_slot == "level"
    assert service.handle("s", "本科生").next_slot == "enrollment_status"
    assert service.handle("s", "在校生").next_slot == "purpose"
    assert service.handle("s", "出国留学").next_slot == "delivery"
    reply = service.handle("s", "自助打印")
    assert reply.next_slot is None
    assert "5 份内免费" in reply.text
    assert "24 小时" in reply.text
    assert "自带信封" in reply.text


def test_certificate_graduate_handoff() -> None:
    service = make_service(date(2026, 10, 25))
    service.handle("s", "研究生在读证明")
    service.handle("s", "在校生")
    reply = service.handle("s", "就业")
    assert "研究生院培养科" in reply.text
    assert "020-85280189" in reply.text
    assert "以培养科" in reply.text
    assert "5 份内免费" not in reply.text


def test_certificate_undergraduate_graduated_not_applicable() -> None:
    service = make_service(date(2026, 10, 25))
    service.handle("s", "我要开在读证明")
    service.handle("s", "本科生")
    reply = service.handle("s", "已毕业")
    assert "学历学位证明" in reply.text
    assert "15 元" in reply.text


def test_recommendation_closed_but_receiving_stage_visible() -> None:
    service = make_service(date(2026, 10, 25))
    reply = service.handle("s", "我大四，已经拿到推免资格，还需要统考报名吗？")
    assert reply.closed is True
    assert "接收阶段" in reply.text
    assert "yz.chsi.com.cn/tm" in reply.text
    assert "不需要再参加" in reply.text


def test_recommendation_open_before_deadline() -> None:
    service = make_service(date(2026, 9, 10))
    reply = service.handle("s", "我大四，学院有推免名额，还在名单里")
    assert reply.closed is False
    assert reply.status == "open"
    assert "放弃推免资格" in reply.text
    assert "接收阶段" in reply.text

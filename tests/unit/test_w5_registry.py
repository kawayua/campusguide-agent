from __future__ import annotations

from datetime import date

from campusguide.domain.enums import Audience
from campusguide.domain.models import FlowDefinition, FlowStep, HandoffInfo
from campusguide.flows.registry import FlowRegistry, match_flows, match_is_ambiguous


def make_flow(flow_id: str, title: str, aliases: list[str], audience=None):
    return FlowDefinition(
        flow_id=flow_id,
        version="v2026",
        title=title,
        description="d",
        audience=audience or [Audience.UNDERGRADUATE],
        aliases=aliases,
        effective_from=date(2026, 9, 1),
        steps=[
            FlowStep(
                id="s1",
                title="步骤",
                description="d",
                deadline_note="以通知为准",
                responsible_department="学生工作部",
                citations=["doc@v1#a"],
            )
        ],
        handoff=HandoffInfo(department="学生工作部"),
    )


def test_registry_selects_active_version_and_audience() -> None:
    flow = make_flow("scholarship", "奖学金申请", ["奖学金"])
    registry = FlowRegistry([flow])
    assert registry.get_flow("scholarship", as_of=date(2026, 10, 25)) is flow
    assert registry.get_flow("scholarship", as_of=date(2026, 8, 1)) is None
    assert registry.active_flows(as_of=date(2026, 10, 25), audience=Audience.GRADUATE) == []
    assert registry.active_flows(as_of=date(2026, 10, 25), audience=Audience.UNDERGRADUATE) == [
        flow
    ]


def test_match_alias_and_title() -> None:
    scholarship = make_flow("scholarship", "奖学金申请", ["奖学金", "国家奖学金"])
    certificate = make_flow("certificate", "在读证明开具", ["在读证明", "在校证明"])
    matches = match_flows("我想申请国家奖学金", [scholarship, certificate])
    assert len(matches) == 1 and matches[0].flow.flow_id == "scholarship"
    matches = match_flows("我要开在读证明", [scholarship, certificate])
    assert len(matches) == 1 and matches[0].flow.flow_id == "certificate"


def test_match_ambiguity_and_empty() -> None:
    a = make_flow("a", "A 流程", ["办理"])
    b = make_flow("b", "B 流程", ["办理"])
    matches = match_flows("我要办理", [a, b])
    assert match_is_ambiguous(matches)
    assert len(matches) == 2
    assert match_flows("今天天气不错", [a, b]) == []


def test_longer_alias_wins() -> None:
    a = make_flow("a", "奖学金申请", ["奖学金"])
    b = make_flow("b", "国家奖学金", ["国家奖学金"])
    matches = match_flows("国家奖学金怎么申请", [a, b])
    assert len(matches) == 1
    assert matches[0].flow.flow_id == "b"

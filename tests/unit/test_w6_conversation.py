from __future__ import annotations

from datetime import date

from campusguide.conversation.service import ConversationService
from tests.w6_helpers import (
    article,
    claims_json,
    make_runner,
    supported_json,
)


def quote() -> str:
    return article("national-aid-undergrad@v2021#第九条").text[:70]


def service(as_of: date = date(2026, 10, 25)) -> ConversationService:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    return ConversationService(None, graph_runner=runner, today_provider=lambda: as_of)


def test_graph_facade_returns_claims_and_trace() -> None:
    svc = service()
    reply = svc.handle("s1", "申请国家奖学金需要满足哪些条件？")
    assert reply.outcome == "ANSWERED"
    assert reply.claims
    assert reply.citations
    assert reply.decision == "pass"
    assert reply.trace
    assert reply.handoff


def test_graph_facade_clarify_exposes_next_slot() -> None:
    svc = service(date(2026, 9, 30))
    reply = svc.handle("s2", "我要申请国家奖学金", audience=None)
    assert reply.outcome == "CLARIFY"
    assert reply.next_slot == "college"
    assert reply.flow_id == "scholarship-application"
    assert reply.clarification is not None


def test_graph_facade_cancel_resets_thread() -> None:
    svc = service(date(2026, 9, 30))
    first = svc.handle("s3", "数信学院大二申请国奖")
    assert first.next_slot == "has_record"
    cancelled = svc.handle("s3", "重来")
    assert "已退出" in cancelled.text
    fresh = svc.handle("s3", "数信学院大二申请国奖")
    assert fresh.next_slot == "has_record"
    assert fresh.trace[0]["node"] == "planner"


def test_graph_facade_empty_message() -> None:
    svc = service()
    reply = svc.handle("s4", "   ")
    assert "请描述" in reply.text


def test_graph_facade_requires_registry_or_runner() -> None:
    try:
        ConversationService()
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("缺少 registry/graph_runner 应当报错")

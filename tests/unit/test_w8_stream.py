"""W8 handle_stream 契约：只发节点进度，final 才携带 reply。"""

from __future__ import annotations

import time
from datetime import date
from types import SimpleNamespace

import pytest

from campusguide.agents.graph import GraphStreamEvent
from campusguide.conversation.service import (
    ConversationEvent,
    ConversationReply,
    ConversationService,
)


class FakeStreamRunner:
    def __init__(self) -> None:
        self.reset_ids: list[str] = []

    def stream(self, session_id: str, message: str, **kwargs):
        yield GraphStreamEvent(kind="progress", node="planner")
        yield GraphStreamEvent(kind="progress", node="retriever")
        yield GraphStreamEvent(kind="progress", node="verifier")
        yield GraphStreamEvent(
            kind="final",
            state={
                "final_answer": "已核实回答文本",
                "outcome": {"code": "ANSWERED"},
                "decision": None,
                "flow_id": None,
                "flow_status": None,
                "checklist": [],
                "missing_slots": [],
                "pending_flow_ids": [],
                "retrieval_warnings": [],
                "claims": [],
                "citations": [],
                "handoff": {},
                "trace": [{"node": "finalize", "duration_ms": 1}],
                "clarification": None,
            },
        )

    def reset(self, session_id: str) -> None:
        self.reset_ids.append(session_id)


def _service(runner: FakeStreamRunner) -> ConversationService:
    return ConversationService(
        None,
        graph_runner=runner,
        today_provider=lambda: date(2026, 10, 25),
    )


def test_handle_stream_progress_then_final() -> None:
    runner = FakeStreamRunner()
    service = _service(runner)
    events = list(service.handle_stream("s1", "国家奖学金怎么申请"))
    assert [event.kind for event in events] == ["progress", "progress", "progress", "final"]
    assert [event.label for event in events[:3]] == ["规划", "检索", "核对"]
    final = events[-1]
    assert final.reply is not None
    assert final.reply.outcome == "ANSWERED"
    assert final.reply.text == "已核实回答文本"


def test_handle_stream_empty_and_reset_are_final_only() -> None:
    runner = FakeStreamRunner()
    service = _service(runner)
    empty = list(service.handle_stream("s1", "  "))
    assert len(empty) == 1
    assert empty[0].kind == "final"
    assert empty[0].reply is not None
    assert "请描述" in empty[0].reply.text

    reset = list(service.handle_stream("s1", "重来"))
    assert len(reset) == 1
    assert reset[0].kind == "final"
    assert runner.reset_ids == ["s1"]


def test_graph_backend_maps_service_stream_to_ui_events() -> None:
    from campusguide.ui.backends import GraphBackend, ProgressEvent

    class FakeService:
        def handle_stream(self, thread_id: str, message: str, **kwargs):
            yield ConversationEvent(kind="progress", node="planner", label="规划")
            yield ConversationEvent(
                kind="final",
                reply=ConversationReply(text="ok", outcome="ANSWERED"),
            )

    backend = GraphBackend(as_of=date(2026, 10, 25))
    backend._runtime = SimpleNamespace(  # type: ignore[attr-defined]
        service=FakeService(),
        model_profile="ultraspeed",
        models={"planner": "fake"},
        prompt_versions={},
        index_version="idx",
    )
    events = list(backend.stream("t1", "hi"))
    assert isinstance(events[0], ProgressEvent)
    assert events[0].label == "正在准备检索"
    assert events[1] == ProgressEvent("planner", "规划")
    assert isinstance(events[-1], ConversationReply)
    assert events[-1].text == "ok"
    assert backend.profile == "ultraspeed"


def test_graph_backend_times_out_and_guards_same_thread() -> None:
    from campusguide.ui.backends import GraphBackend, ProgressEvent

    class SlowThenFastService:
        def __init__(self) -> None:
            self.calls = 0

        def handle_stream(self, thread_id: str, message: str, **kwargs):
            self.calls += 1
            yield ConversationEvent(kind="progress", node="planner", label="规划")
            if self.calls == 1:
                time.sleep(0.5)
            yield ConversationEvent(
                kind="final",
                reply=ConversationReply(text="ok", outcome="ANSWERED"),
            )

    service = SlowThenFastService()
    backend = GraphBackend(as_of=date(2026, 10, 25), request_timeout_seconds=0.1)
    backend._runtime = SimpleNamespace(  # type: ignore[attr-defined]
        service=service,
        model_profile="ultraspeed",
        models={},
        prompt_versions={},
        index_version="idx",
    )
    seen = []
    with pytest.raises(TimeoutError):
        for event in backend.stream("t-timeout", "hi"):
            seen.append(event)
    assert isinstance(seen[0], ProgressEvent)
    with pytest.raises(RuntimeError, match="尚未结束"):
        next(backend.stream("t-timeout", "again"))

    time.sleep(0.6)
    events = list(backend.stream("t-timeout", "after"))
    assert isinstance(events[-1], ConversationReply)
    assert events[-1].text == "ok"


def test_graph_backend_metadata_exposes_timeout_and_warmup() -> None:
    from campusguide.ui.backends import GraphBackend

    backend = GraphBackend(request_timeout_seconds=12)
    metadata = backend.metadata()
    assert metadata["request_timeout_seconds"] == 12
    assert metadata["warmup_ms"] == 0


def test_graph_backend_display_profile_can_differ_from_run_profile(monkeypatch) -> None:
    from campusguide.ui.backends import GraphBackend

    monkeypatch.setenv("W8_DEMO_PROFILE", "ultraspeed")
    monkeypatch.setenv("W6_MODEL_PROFILE", "vpro")
    backend = GraphBackend()
    assert backend.profile == "ultraspeed"
    backend._runtime = SimpleNamespace(  # type: ignore[attr-defined]
        model_profile="vpro",
        models={"planner": "mimo-v2.6-pro"},
        prompt_versions={},
        index_version="idx",
        service=SimpleNamespace(),
    )
    assert backend.profile == "ultraspeed"
    metadata = backend.metadata()
    assert metadata["model_profile"] == "vpro"
    assert metadata["display_profile"] == "ultraspeed"

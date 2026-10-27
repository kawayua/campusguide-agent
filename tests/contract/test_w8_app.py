"""W8 Gradio 契约：fake runner 下可构建、可发送、可切换历史。"""

from __future__ import annotations

import time
from typing import Any

import gradio as gr

from campusguide.ui.app import W8DemoUI, build_demo
from campusguide.ui.fake_backend import FakeBackend


class SpyBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def stream(self, thread_id: str, message: str):
        self.calls.append((thread_id, message))
        yield from super().stream(thread_id, message)


def _ui(backend: Any | None = None) -> W8DemoUI:
    ui = W8DemoUI(backend or FakeBackend())
    ui.build()
    return ui


def _drain(generator) -> list[tuple[Any, ...]]:
    return list(generator)


def _final_store(generator) -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
    payloads = list(generator)
    assert payloads
    return payloads[-1][0], payloads


def test_build_demo_with_fake_runner() -> None:
    demo = build_demo(FakeBackend())
    assert isinstance(demo, gr.Blocks)


def test_initial_load_creates_one_session_and_welcome() -> None:
    ui = _ui()
    payload = ui.on_load(None)
    assert len(payload) == 22
    store = payload[0]
    session_id = store["current_id"]
    assert store["order"] == [session_id]
    assert payload[1]["visible"] is False  # chatbot hidden until first message
    assert payload[2]["visible"] is True  # welcome visible
    assert payload[3]["value"] == session_id
    assert payload[5]["visible"] is False  # result panel closed
    assert "新对话" in payload[4]


def test_empty_and_overlong_input_do_not_call_backend() -> None:
    backend = SpyBackend()
    ui = _ui(backend)
    store = ui.on_load(None)[0]
    session_id = store["current_id"]

    empty = _drain(ui.on_send(store, "   "))
    assert len(empty) == 1
    assert empty[0][17]["visible"] is True
    assert backend.calls == []
    assert store["sessions"][session_id]["messages"] == []

    long = _drain(ui.on_send(store, "问" * 301))
    assert len(long) == 1
    assert long[0][17]["visible"] is True
    assert backend.calls == []


def test_rate_limit_blocks_backend_without_appending() -> None:
    backend = SpyBackend()
    ui = _ui(backend)
    store = ui.on_load(None)[0]
    store["requests"] = [time.time()] * 10
    payloads = list(ui.on_send(store, "国家奖学金怎么申请"))
    assert len(payloads) == 1
    assert payloads[0][17]["visible"] is True
    session = store["sessions"][store["current_id"]]
    assert session["messages"] == []
    assert backend.calls == []


def test_fake_send_updates_history_panel_and_store() -> None:
    backend = SpyBackend()
    ui = _ui(backend)
    store = ui.on_load(None)[0]
    session_id = store["current_id"]

    final_store, payloads = _final_store(ui.on_send(store, "国家奖学金怎么申请"))
    session = final_store["sessions"][session_id]
    assert backend.calls == [(session["thread_id"], "国家奖学金怎么申请")]
    assert session["title"] == "国家奖学金怎么申请"
    assert session["outcome"] == "ANSWERED"
    assert "2 步" in session["preview"]
    assert len(session["messages"]) == 2
    assert session["reply"]["closed"] is True
    assert payloads[-1][1]["visible"] is True
    assert payloads[-1][5]["visible"] is True
    assert "数信学院：国家奖学金申请" in payloads[-1][8]
    assert "scholarship" not in payloads[-1][6].lower()


def test_clarify_buttons_complete_certificate_flow() -> None:
    ui = _ui()
    store = ui.on_load(None)[0]
    session_id = store["current_id"]

    store, payloads = _final_store(ui.on_send(store, "我要开在读证明"))
    session = store["sessions"][session_id]
    assert session["outcome"] == "CLARIFY"
    assert session["reply"]["clarification"]["allowed_values"] == [
        "undergraduate",
        "graduate",
    ]
    assert payloads[-1][12]["value"] == "本科生"
    assert payloads[-1][16]["visible"] is True  # skip

    store, payloads = _final_store(ui.on_clarify(store, 0))
    assert store["sessions"][session_id]["outcome"] == "CLARIFY"
    assert "在校生" in payloads[-1][12]["value"]

    store, payloads = _final_store(ui.on_clarify(store, 0))
    session = store["sessions"][session_id]
    assert session["outcome"] == "ANSWERED"
    assert len(session["reply"]["checklist"]) == 2
    assert "自助打印" in payloads[-1][8]


def test_new_chat_and_history_switch_restores_panel() -> None:
    ui = _ui()
    store = ui.on_load(None)[0]
    first_id = store["current_id"]
    store, _ = _final_store(ui.on_send(store, "国家奖学金怎么申请"))

    store = ui.on_new_chat(store)[0]
    second_id = store["current_id"]
    assert second_id != first_id
    store, _ = _final_store(ui.on_send(store, "查一下我的绩点和排名"))
    assert store["sessions"][second_id]["outcome"] == "PRIVACY_REQUEST"

    payload = ui.on_select(store, first_id)
    store = payload[0]
    assert store["current_id"] == first_id
    assert payload[1]["visible"] is True
    assert payload[5]["visible"] is True
    assert "国家奖学金怎么申请" in payload[4]
    assert "数信学院：国家奖学金申请" in payload[8]


def test_panel_components_and_checklist_filter() -> None:
    ui = _ui()
    store = ui.on_load(None)[0]
    store, payload = _final_store(ui.on_send(store, "国家奖学金怎么申请"))

    assert ui.panel_toggle is not None
    assert payload[-1][5]["visible"] is True
    assert len(payload[-1]) == 22

    store, html = ui.on_filter(store, "closed", False)
    assert store["ui"]["status_filter"] == "closed"
    assert "数信学院：国家奖学金申请" in html
    assert "农学院：国家奖学金（以学院通知为准）" not in html


def test_search_filters_history_choices() -> None:
    ui = _ui()
    store = ui.on_load(None)[0]
    store = ui.on_new_chat(store)[0]
    store, _ = _final_store(ui.on_send(store, "查一下我的绩点和排名"))
    update = ui.on_search(store, "绩点")
    values = {value for _, value in update["choices"]}
    assert store["current_id"] in values

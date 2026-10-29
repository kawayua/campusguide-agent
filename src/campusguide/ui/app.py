"""W8 三栏 Gradio 界面：历史 / 对话 / 结构化结果。

UI 只消费 ``ConversationReply``；真实图后端惰性初始化。测试与本地预览
通过 ``FakeBackend`` 注入，不加载模型或网络。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

import gradio as gr

from campusguide.env import load_dotenv
from campusguide.ui.backends import DemoBackend, GraphBackend, ProgressEvent
from campusguide.ui.fake_backend import FakeBackend
from campusguide.ui.store import (
    add_session,
    append_user_message,
    apply_error,
    apply_reply,
    ensure_current,
    new_session,
    normalize_store,
    reset_session,
    session_choices,
    session_meta,
    session_trace_id,
)
from campusguide.ui.theme import THEME_CSS
from campusguide.ui.view_models import (
    assistant_message_html,
    checklist_html,
    citations_html,
    clarify_options,
    clarify_prompt_html,
    conversation_header_html,
    debug_html,
    progress_html,
    side_header_html,
    visible_flow_step_count,
)

DEFAULT_PROFILE = "ultraspeed"
BRAND_HTML = """
<div class="w8-brand">
  <div class="w8-brand-mark">CG</div>
  <div><h1>CampusGuide</h1><p>华农制度问答</p></div>
  <button class="w8-history-close" type="button" aria-label="关闭历史记录">×</button>
</div>
"""
HISTORY_FOOTER = """
<div class="w8-history-footer">
  <div class="w8-history-foot-row">
    <span class="w8-profile-badge"><span class="w8-dot"></span>本机保留 · 刷新不丢失</span>
  </div>
  <div class="w8-history-note">
    <span>数据截止 2026-10-25 · 华南农业大学公开制度</span>
    <span>演示系统不构成官方答复</span>
    <span class="w8-privacy">请勿输入学号 / 身份证 / 成绩单</span>
  </div>
</div>
"""
FORCE_LIGHT_JS = """
() => {
  const force = () => {
    document.documentElement.classList.remove('dark');
    document.body.classList.remove('dark');
    document.documentElement.style.colorScheme = 'light';
  };
  force();
  const observer = new MutationObserver(force);
  observer.observe(document.documentElement, {attributes:true, attributeFilter:['class']});
  observer.observe(document.body, {attributes:true, attributeFilter:['class']});
  setTimeout(() => observer.disconnect(), 5000);
  document.addEventListener('click', (event) => {
    if (event.target.closest('.w8-history-close')) {
      document.getElementById('w8-history')?.classList.remove('mobile-open');
    }
  });
}
"""
PANEL_TOGGLE_JS = """
() => {
  const side = document.getElementById('w8-side');
  if (!side) return;
  if (window.matchMedia('(max-width: 1100px)').matches) {
    side.classList.toggle('mobile-open');
  } else {
    side.classList.toggle('panel-hidden');
  }
}
"""
OPEN_RESULT_JS = """
() => {
  document.getElementById('w8-history')?.classList.remove('mobile-open');
  const side = document.getElementById('w8-side');
  if (!side) return;
  side.classList.remove('panel-hidden');
  if (window.matchMedia('(max-width: 1100px)').matches) {
    side.classList.add('mobile-open');
  }
}
"""
CLOSE_RESULT_JS = """
() => {
  document.getElementById('w8-history')?.classList.remove('mobile-open');
  document.getElementById('w8-side')?.classList.remove('mobile-open');
}
"""
RESET_RESULT_JS = """
() => {
  document.getElementById('w8-history')?.classList.remove('mobile-open');
  const side = document.getElementById('w8-side');
  side?.classList.remove('mobile-open');
  side?.classList.remove('panel-hidden');
}
"""
COMPOSER_META = (
    '<div class="w8-composer-meta">'
    '<span class="w8-privacy">● 不查询个人信息</span>'
    "<span>单次 ≤300 字 · 每分钟 10 次</span>"
    "<span>演示系统不构成官方答复 · 数据截止 2026-10-25</span>"
    "</div>"
)


class W8DemoUI:
    def __init__(self, backend: DemoBackend | None = None) -> None:
        self.backend = backend or GraphBackend()
        self.demo = None
        self.store = None
        self.new_chat_btn = None
        self.search = None
        self.reset_btn = None
        self.chatbot = None
        self.welcome = None
        self.history = None
        self.history_toggle = None
        self.header = None
        self.panel_toggle = None
        self.side_col = None
        self.side_header = None
        self.tabs = None
        self.checklist = None
        self.citations = None
        self.debug = None
        self.clarify_prompt = None
        self.clarify_row = None
        self.opts: list[Any] = []
        self.skip = None
        self.status = None
        self.input = None
        self.send_btn = None
        self.filter = None
        self.show_na = None
        self.examples: list[tuple[Any, str]] = []

    # ----------------------------------------------------------- UI assembly
    def build(self) -> gr.Blocks:
        with gr.Blocks(
            title="CampusGuide · 华农制度问答",
            fill_height=True,
        ) as demo:
            self.store = gr.BrowserState(None, storage_key="campusguide_w8_ui_v1")

            with gr.Row(elem_id="w8-root"):
                with gr.Column(elem_id="w8-history", scale=0, min_width=250):
                    gr.HTML(BRAND_HTML, elem_id="w8-brand")
                    self.new_chat_btn = gr.Button(
                        "＋ 新对话", elem_id="w8-new-chat", variant="secondary"
                    )
                    self.search = gr.Textbox(
                        placeholder="搜索对话",
                        show_label=False,
                        container=False,
                        elem_id="w8-history-search",
                    )
                    self.history = gr.Radio(
                        choices=[],
                        value=None,
                        show_label=False,
                        container=False,
                        elem_id="w8-history-list",
                    )
                    gr.HTML(HISTORY_FOOTER, elem_id="w8-history-footer")
                    self.reset_btn = gr.Button("重置当前会话", size="sm", elem_id="w8-reset")

                with gr.Column(elem_id="w8-chat", scale=1, min_width=420):
                    with gr.Row(elem_id="w8-chat-head"):
                        self.history_toggle = gr.Button(
                            "☰", size="sm", elem_id="w8-history-toggle", min_width=30
                        )
                        self.header = gr.HTML(
                            conversation_header_html(
                                title="新对话",
                                meta="选择左侧历史，或直接提问",
                                outcome=None,
                                closed=False,
                                profile=self._profile_label(),
                            ),
                            elem_id="w8-conversation-head",
                        )
                        self.panel_toggle = gr.Button("结果", size="sm", elem_id="w8-panel-toggle")

                    self.chatbot = gr.Chatbot(
                        value=[],
                        show_label=False,
                        container=False,
                        sanitize_html=False,
                        render_markdown=True,
                        layout="bubble",
                        height="100%",
                        buttons=[],
                        feedback_options=None,
                        allow_file_downloads=False,
                        elem_id="w8-chatbot",
                    )
                    self.welcome = gr.HTML(self._welcome_html(), elem_id="w8-welcome")
                    self.status = gr.HTML("", elem_id="w8-status", visible=False)
                    self.clarify_prompt = gr.HTML("", elem_id="w8-clarify-prompt", visible=False)
                    with gr.Row(elem_id="w8-clarify-row") as self.clarify_row:
                        self.opts = [
                            gr.Button("", visible=False, size="sm", elem_id=f"w8-opt-{i}")
                            for i in range(4)
                        ]
                        self.skip = gr.Button("跳过", visible=False, size="sm", elem_id="w8-skip")

                    with gr.Row(elem_id="w8-examples"):
                        for label, question in (
                            ("国奖条件", "申请国家奖学金需要满足哪些条件？"),
                            ("国奖流程", "国家奖学金怎么申请"),
                            ("在读证明", "我要开在读证明"),
                            ("推免", "推免资格怎么确认？"),
                            ("隐私测试", "查一下我的绩点和排名"),
                        ):
                            self._example_button(label, question)

                    with gr.Row(elem_id="w8-composer"):
                        self.input = gr.Textbox(
                            placeholder="问制度条件、办事流程、材料或截止时间……",
                            show_label=False,
                            container=False,
                            lines=2,
                            max_lines=4,
                            max_length=300,
                            elem_id="w8-input",
                        )
                        self.send_btn = gr.Button(
                            "→", variant="primary", elem_id="w8-send", interactive=False
                        )
                    gr.HTML(COMPOSER_META, elem_id="w8-composer-meta")

                with gr.Column(elem_id="w8-side", scale=0, min_width=420, visible=False) as side:
                    self.side_col = side
                    self.side_header = gr.HTML(side_header_html(None), elem_id="w8-side-header")
                    with gr.Tabs(selected="checklist", elem_id="w8-tabs") as tabs:
                        self.tabs = tabs
                        with gr.Tab("办理清单", id="checklist"):
                            with gr.Row(elem_id="w8-filters"):
                                self.filter = gr.Dropdown(
                                    choices=[
                                        ("全部", "all"),
                                        ("已截止", "closed"),
                                        ("受阻", "blocked"),
                                        ("待确认", "conditional"),
                                        ("可办理", "available"),
                                        ("已完成", "done"),
                                    ],
                                    value="all",
                                    show_label=False,
                                    container=False,
                                    elem_id="w8-filter",
                                )
                                self.show_na = gr.Checkbox(
                                    label="显示不适用项",
                                    value=False,
                                    container=False,
                                    elem_id="w8-show-na",
                                )
                            self.checklist = gr.HTML(checklist_html(None), elem_id="w8-checklist")
                        with gr.Tab("引用", id="citations"):
                            self.citations = gr.HTML(citations_html(None), elem_id="w8-citations")
                        with gr.Tab("调试", id="debug"):
                            self.debug = gr.HTML(debug_html(None), elem_id="w8-debug")

            self.demo = demo
            self._wire()
        return demo

    def _example_button(self, label: str, question: str) -> None:
        button = gr.Button(label, size="sm", elem_id=f"w8-example-{label}")
        self.examples.append((button, question))

    def _make_example_handler(self, question: str):
        def handler(store: dict[str, Any]):
            yield from self.on_send(store, question)

        return handler

    def _profile_label(self) -> str:
        return str(getattr(self.backend, "profile", DEFAULT_PROFILE) or DEFAULT_PROFILE)

    def _metadata(self) -> dict[str, Any]:
        try:
            return self.backend.metadata()
        except Exception:
            return {}

    def _outputs(self) -> list[Any]:
        return [
            self.store,
            self.chatbot,
            self.welcome,
            self.history,
            self.header,
            self.side_col,
            self.side_header,
            self.tabs,
            self.checklist,
            self.citations,
            self.debug,
            self.clarify_prompt,
            *self.opts,
            self.skip,
            self.status,
            self.input,
            self.send_btn,
            self.filter,
            self.show_na,
        ]

    # ----------------------------------------------------------- UI state
    @staticmethod
    def _ui(store: dict[str, Any]) -> dict[str, Any]:
        ui = store.get("ui")
        if not isinstance(ui, dict):
            ui = {}
            store["ui"] = ui
        ui.setdefault("show_na", False)
        ui.setdefault("status_filter", "all")
        return ui

    @staticmethod
    def _welcome_html() -> str:
        return """
        <div class="w8-welcome">
          <div class="w8-welcome-card">
            <div class="w8-welcome-kicker">CAMPUSGUIDE · 公开制度问答</div>
            <h2>先问制度，再办事。</h2>
            <p>答案只由通过硬校验与语义核对的结论组成；流程事实由办理清单渲染。
            涉及个人成绩、学号、身份证的请求会被明确拒绝。</p>
            <div class="w8-welcome-grid">
              <div><b>01</b><span>政策条件：年级、成绩、资格、金额</span></div>
              <div><b>02</b><span>办事流程：材料、地点、截止时间</span></div>
              <div><b>03</b><span>多轮澄清：只问关键的下一项</span></div>
              <div><b>04</b><span>引用与调试：每条结论可定位</span></div>
            </div>
            <div class="w8-welcome-tags">
              <span>数据截止 2026-10-25</span><span>华南农业大学 · 公开制度</span>
              <span>不查询个人信息</span><span>本地演示</span>
            </div>
          </div>
        </div>
        """

    @staticmethod
    def _side_sub(reply: dict[str, Any] | None) -> str:
        if not reply:
            return ""
        code = str(reply.get("outcome") or "")
        visible_steps = visible_flow_step_count(reply)
        if visible_steps:
            closed = " · 批次已截止" if reply.get("closed") else ""
            return f"{visible_steps} 个可见步骤{closed}"
        claims = list(reply.get("claims") or [])
        citations = list(reply.get("citations") or [])
        if claims:
            return f"{len(claims)} 条结论 · {len(citations)} 条引用"
        if code == "CLARIFY":
            missing = (reply.get("clarification") or {}).get("missing_slots") or []
            return f"还缺 {len(missing)} 项信息" if missing else "等待补充信息"
        if code == "PRIVACY_REQUEST":
            return "涉及个人信息 · 未查询"
        if code == "OUT_OF_SCOPE":
            return "超出华农公开制度范围"
        return ""

    def _pack(
        self,
        store: dict[str, Any],
        *,
        side_visible: bool,
        status: str = "",
        status_visible: bool = False,
        progress_nodes: list[str] | None = None,
        clear_input: bool = False,
        send_interactive: bool = False,
    ) -> tuple[Any, ...]:
        store, session = ensure_current(store)
        ui = self._ui(store)
        reply = session.get("reply")
        choices = session_choices(store)
        current = store.get("current_id")
        messages = list(session.get("messages") or [])
        outcome = str(session.get("outcome") or "")
        closed = bool((reply or {}).get("closed")) if isinstance(reply, dict) else False
        title = str(session.get("title") or "新对话")
        meta = session_meta(session, profile=self._profile_label())
        if status_visible:
            meta = "正在处理 · 只流式节点进度"
        header = conversation_header_html(
            title=title,
            meta=meta,
            outcome=outcome if not status_visible else None,
            closed=closed,
            profile=self._profile_label(),
        )
        side_head = side_header_html(reply, sub=self._side_sub(reply))
        active_tab = str(session.get("active_tab") or "checklist")
        checklist = checklist_html(
            reply,
            show_not_applicable=bool(ui.get("show_na")),
            status_filter=str(ui.get("status_filter") or "all"),
        )
        citations = citations_html(reply)
        debug = debug_html(
            reply,
            metadata=self._metadata(),
            trace_id=session_trace_id(session),
        )
        prompt = clarify_prompt_html(reply)
        options = clarify_options(reply)
        is_clarify = bool(reply) and str((reply or {}).get("outcome")) == "CLARIFY"
        option_updates = []
        for index in range(4):
            if index < len(options):
                label, _ = options[index]
                option_updates.append(gr.update(value=label, visible=True))
            else:
                option_updates.append(gr.update(visible=False))
        skip_update = gr.update(visible=is_clarify)
        status_update = (
            gr.update(value=progress_html(status, nodes=progress_nodes), visible=True)
            if status_visible
            else gr.update(visible=False)
        )
        return (
            store,
            gr.update(value=messages, visible=bool(messages)),
            gr.update(value=self._welcome_html(), visible=not messages),
            gr.update(choices=choices, value=current),
            header,
            gr.update(visible=side_visible),
            side_head,
            gr.update(selected=active_tab),
            checklist,
            citations,
            debug,
            gr.update(value=prompt, visible=bool(prompt)),
            *option_updates,
            skip_update,
            status_update,
            gr.update(value="") if clear_input else gr.update(),
            gr.update(interactive=send_interactive),
            gr.update(value=str(ui.get("status_filter") or "all")),
            gr.update(value=bool(ui.get("show_na"))),
        )

    # ----------------------------------------------------------- events
    def on_load(self, store: Any):
        store, session = ensure_current(normalize_store(store))
        return self._pack(
            store,
            side_visible=bool(session.get("reply")),
            send_interactive=False,
        )

    def on_new_chat(self, store: Any):
        store = normalize_store(store)
        session = new_session()
        add_session(store, session)
        return self._pack(store, side_visible=False, clear_input=True, send_interactive=False)

    def on_reset(self, store: Any):
        store = normalize_store(store)
        store, session = ensure_current(store)
        with contextlib.suppress(Exception):
            self.backend.reset(str(session.get("thread_id") or session["id"]))
        reset_session(session)
        return self._pack(store, side_visible=False, clear_input=True, send_interactive=False)

    def on_select(self, store: Any, session_id: str | None):
        store = normalize_store(store)
        if not session_id or session_id not in store["sessions"]:
            store, session = ensure_current(store)
        else:
            store["current_id"] = session_id
            session = store["sessions"][session_id]
        return self._pack(
            store,
            side_visible=bool(session.get("reply")),
            send_interactive=False,
        )

    def on_search(self, store: Any, query: str):
        store = normalize_store(store)
        current = store.get("current_id")
        q = str(query or "").strip().lower()
        choices = []
        for label, session_id in session_choices(store):
            if not q or q in label.lower():
                choices.append((label, session_id))
        values = {value for _, value in choices}
        return gr.update(choices=choices, value=current if current in values else None)

    def on_filter(self, store: Any, status_filter: str, show_na: bool):
        store = normalize_store(store)
        store, session = ensure_current(store)
        ui = self._ui(store)
        ui["status_filter"] = status_filter or "all"
        ui["show_na"] = bool(show_na)
        return store, checklist_html(
            session.get("reply"),
            show_not_applicable=ui["show_na"],
            status_filter=ui["status_filter"],
        )

    def on_send(self, store: Any, message: str) -> Iterator[tuple[Any, ...]]:
        store = normalize_store(store)
        store, session = ensure_current(store)
        text = str(message or "").strip()
        if not text:
            yield self._pack(
                store,
                side_visible=bool(session.get("reply")),
                status="请输入问题后再发送。",
                status_visible=True,
                send_interactive=False,
            )
            return
        if len(text) > 300:
            yield self._pack(
                store,
                side_visible=bool(session.get("reply")),
                status="单次输入不能超过 300 字。",
                status_visible=True,
                send_interactive=False,
            )
            return
        if not self._rate_allowed(store):
            yield self._pack(
                store,
                side_visible=bool(session.get("reply")),
                status="本分钟演示额度已用完（10 次），请稍后再试。",
                status_visible=True,
                send_interactive=False,
            )
            return

        append_user_message(session, text)
        yield self._pack(
            store,
            side_visible=bool(session.get("reply")),
            status="正在规划与核对",
            status_visible=True,
            progress_nodes=[],
            clear_input=True,
            send_interactive=False,
        )

        thread_id = str(session.get("thread_id") or session["id"])
        reply = None
        seen_labels: list[str] = []
        seen_nodes: list[str] = []
        try:
            for event in self.backend.stream(thread_id, text):
                if isinstance(event, ProgressEvent):
                    seen_labels.append(event.label)
                    seen_nodes.append(event.node)
                    yield self._pack(
                        store,
                        side_visible=bool(session.get("reply")),
                        status=" → ".join(seen_labels),
                        status_visible=True,
                        progress_nodes=seen_nodes,
                        send_interactive=False,
                    )
                else:
                    reply = event
            if reply is None:
                raise RuntimeError("后端未返回最终答复")
        except Exception as exc:
            message_text = self._error_text(exc)
            apply_error(session, message_text)
            yield self._pack(
                store,
                side_visible=False,
                status=message_text,
                status_visible=True,
                send_interactive=False,
            )
            return

        assistant_html = assistant_message_html(reply)
        apply_reply(session, reply, assistant_html, trace_id=thread_id)
        yield self._pack(
            store,
            side_visible=True,
            send_interactive=False,
        )

    @staticmethod
    def _rate_allowed(store: dict[str, Any]) -> bool:
        now = time.time()
        recent = [float(item) for item in store.get("requests", []) if now - float(item) < 60]
        if len(recent) >= 10:
            store["requests"] = recent
            return False
        recent.append(now)
        store["requests"] = recent
        return True

    def _error_text(self, exc: Exception) -> str:
        name = type(exc).__name__
        if name in {"TimeoutError", "TimeoutException"}:
            timeout = os.getenv("W8_REQUEST_TIMEOUT_SECONDS", "120").strip() or "120"
            return (
                f"本轮超过 {timeout} 秒，已停止等待；未展示半成品。"
                "请稍后重试，或先用 CLI 检查 Qdrant / MCP / 模型。"
            )
        if "api" in str(exc).lower() and "key" in str(exc).lower():
            return "模型配置缺失或不可用；请检查 `.env` 中的 LLM_BASE_URL / LLM_API_KEY。"
        return "服务暂时不可用；请检查 Qdrant、MCP 与 `.env` 配置后重试。不显示堆栈。"

    def on_clarify(self, store: Any, index: int):
        store = normalize_store(store)
        store, session = ensure_current(store)
        reply = session.get("reply")
        options = clarify_options(reply)
        if 0 <= index < len(options):
            yield from self.on_send(store, options[index][1])
        else:
            yield self._pack(
                store,
                side_visible=bool(reply),
                status="这个选项已经失效，请重新提问。",
                status_visible=True,
            )

    def on_skip(self, store: Any):
        yield from self.on_send(store, "跳过")

    def on_input_change(self, message: str):
        return gr.update(interactive=bool(str(message or "").strip()))

    # ----------------------------------------------------------- wiring
    def _wire(self) -> None:
        outputs = self._outputs()
        self.history_toggle.click(
            fn=None,
            inputs=None,
            outputs=None,
            js="() => { document.getElementById('w8-history')?.classList.toggle('mobile-open'); }",
        )
        self.demo.load(self.on_load, inputs=[self.store], outputs=outputs)
        for button, question in self.examples:
            button.click(
                self._make_example_handler(question),
                inputs=[self.store],
                outputs=outputs,
            ).then(fn=None, js=CLOSE_RESULT_JS)
        self.new_chat_btn.click(
            self.on_new_chat,
            inputs=[self.store],
            outputs=outputs,
        ).then(fn=None, js=RESET_RESULT_JS)
        self.reset_btn.click(
            self.on_reset,
            inputs=[self.store],
            outputs=outputs,
        ).then(fn=None, js=RESET_RESULT_JS)
        self.history.change(
            self.on_select,
            inputs=[self.store, self.history],
            outputs=outputs,
        ).then(fn=None, js=OPEN_RESULT_JS)
        self.search.change(self.on_search, inputs=[self.store, self.search], outputs=[self.history])
        self.panel_toggle.click(fn=None, js=PANEL_TOGGLE_JS)
        self.input.submit(self.on_send, inputs=[self.store, self.input], outputs=outputs).then(
            fn=None, js=CLOSE_RESULT_JS
        )
        self.send_btn.click(self.on_send, inputs=[self.store, self.input], outputs=outputs).then(
            fn=None, js=CLOSE_RESULT_JS
        )
        self.input.change(self.on_input_change, inputs=[self.input], outputs=[self.send_btn])
        for index, button in enumerate(self.opts):
            button.click(
                self._make_clarify_handler(index),
                inputs=[self.store],
                outputs=outputs,
            ).then(fn=None, js=CLOSE_RESULT_JS)
        self.skip.click(self.on_skip, inputs=[self.store], outputs=outputs).then(
            fn=None, js=CLOSE_RESULT_JS
        )
        for component in (self.filter, self.show_na):
            component.change(
                self.on_filter,
                inputs=[self.store, self.filter, self.show_na],
                outputs=[self.store, self.checklist],
            )

    def _make_clarify_handler(self, index: int):
        def handler(store: dict[str, Any]):
            yield from self.on_clarify(store, index)

        return handler


def build_demo(backend: DemoBackend | None = None) -> gr.Blocks:
    return W8DemoUI(backend).build()


def main() -> None:
    parser = argparse.ArgumentParser(description="CampusGuide W8 Gradio UI")
    parser.add_argument("--fake", action="store_true", help="使用无模型 fake backend")
    args = parser.parse_args()
    load_dotenv()
    use_fake = args.fake or os.getenv("DEMO_FAKE", "").strip() in {"1", "true", "yes"}
    backend: DemoBackend = FakeBackend() if use_fake else GraphBackend()
    if isinstance(backend, GraphBackend) and os.getenv(
        "W8_SKIP_WARMUP", ""
    ).strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        try:
            warmup_ms = backend.warmup()
            actual = backend.metadata().get("model_profile", "—")
            print(
                f"[warmup] display={backend.profile} actual={actual} elapsed={warmup_ms}ms",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            print(
                f"[warmup] failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    demo = build_demo(backend)
    demo.queue(default_concurrency_limit=1).launch(
        css=THEME_CSS,
        theme=gr.themes.Base(
            primary_hue="teal",
            neutral_hue="slate",
            radius_size="sm",
        ),
        js=FORCE_LIGHT_JS,
        server_name=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
    )


if __name__ == "__main__":
    main()

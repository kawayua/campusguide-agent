"""W8 UI 会话存储：左侧历史、当前会话与结构化结果的 JSON 契约。

``gr.BrowserState`` 只需要 JSON 友好值；这里统一生成和校验这个结构。
服务端不持久化这份 UI 历史，刷新恢复完全由浏览器 localStorage 完成。
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from typing import Any

from campusguide.ui.view_models import (
    field,
    outcome_meta,
    reply_duration_ms,
    reply_to_dict,
    short_text,
    slot_label,
    visible_flow_step_count,
)

STORE_VERSION = 1


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def empty_store() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "current_id": None,
        "order": [],
        "sessions": {},
    }


def new_session(title: str = "新对话") -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    return {
        "id": session_id,
        "thread_id": session_id,
        "title": title,
        "preview": "开始提问",
        "outcome": "",
        "updated_at": now_iso(),
        "messages": [],
        "reply": None,
        "active_tab": "checklist",
    }


def normalize_store(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_store()
    sessions = value.get("sessions")
    order = value.get("order")
    if not isinstance(sessions, dict) or not isinstance(order, list):
        return empty_store()
    clean: dict[str, Any] = {}
    clean_order: list[str] = []
    for session_id in order:
        session = sessions.get(session_id)
        if not isinstance(session, dict):
            continue
        if not session.get("id"):
            session["id"] = str(session_id)
        if not session.get("thread_id"):
            session["thread_id"] = str(session_id)
        session.setdefault("title", "新对话")
        session.setdefault("preview", "开始提问")
        session.setdefault("outcome", "")
        session.setdefault("updated_at", now_iso())
        session.setdefault("messages", [])
        session.setdefault("reply", None)
        session.setdefault("active_tab", "checklist")
        clean[str(session_id)] = session
        clean_order.append(str(session_id))
    current = value.get("current_id")
    if current not in clean:
        current = clean_order[0] if clean_order else None
    result = {
        "version": STORE_VERSION,
        "current_id": current,
        "order": clean_order,
        "sessions": clean,
    }
    if isinstance(value.get("ui"), dict):
        result["ui"] = dict(value["ui"])
    if isinstance(value.get("requests"), list):
        result["requests"] = [
            float(item) for item in value["requests"] if isinstance(item, (int, float))
        ]
    return result


def ensure_current(store: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    store = normalize_store(store)
    current = store.get("current_id")
    if current and current in store["sessions"]:
        return store, store["sessions"][current]
    if store["order"]:
        store["current_id"] = store["order"][0]
        return store, store["sessions"][store["current_id"]]
    session = new_session()
    store["sessions"][session["id"]] = session
    store["order"].insert(0, session["id"])
    store["current_id"] = session["id"]
    return store, session


def add_session(store: dict[str, Any], session: dict[str, Any], *, select: bool = True) -> None:
    store["sessions"][session["id"]] = session
    if session["id"] in store["order"]:
        store["order"].remove(session["id"])
    store["order"].insert(0, session["id"])
    if select:
        store["current_id"] = session["id"]


def current_session(store: dict[str, Any]) -> dict[str, Any] | None:
    current = store.get("current_id")
    if not current:
        return None
    return store["sessions"].get(current)


def session_glyph(outcome: str | None) -> str:
    code = str(outcome or "")
    if code in {"ANSWERED", "PARTIAL_ANSWER"}:
        return "●"
    if code == "CLARIFY":
        return "○"
    if code:
        return "×"
    return "·"


def session_preview(reply: dict[str, Any] | Any | None) -> str:
    if reply is None:
        return "开始提问"
    code = str(field(reply, "outcome", "") or "")
    meta = outcome_meta(code)
    if code == "CLARIFY":
        clarification = field(reply, "clarification") or {}
        missing = list(field(clarification, "missing_slots", []) or [])
        labels = [slot_label(str(slot)) for slot in missing[:2]]
        return "等待补充：" + " / ".join(labels) if labels else "等待补充信息"
    visible_steps = visible_flow_step_count(reply)
    if visible_steps:
        closed = " · 批次已截止" if field(reply, "closed", False) else ""
        return f"{visible_steps} 步{closed}"
    claims = list(field(reply, "claims", []) or [])
    citations = list(field(reply, "citations", []) or [])
    if claims:
        return f"{len(claims)} 条结论 · {len(citations)} 条引用"
    if code == "PRIVACY_REQUEST":
        return "未作答 · 涉及个人信息"
    if code == "OUT_OF_SCOPE":
        return "未作答 · 超出华农范围"
    if code == "PARTIAL_ANSWER":
        return "部分回答 · 未确认项已标注"
    return meta["label"]


def session_label(session: dict[str, Any]) -> str:
    title = short_text(str(session.get("title") or "新对话"), 20)
    preview = short_text(str(session.get("preview") or "开始提问"), 28)
    return f"{session_glyph(session.get('outcome'))} {title}\n{preview}"


def session_choices(store: dict[str, Any]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for session_id in store.get("order", []):
        session = store.get("sessions", {}).get(session_id)
        if session:
            choices.append((session_label(session), session_id))
    return choices


def append_user_message(session: dict[str, Any], text: str) -> None:
    session.setdefault("messages", []).append({"role": "user", "content": html.escape(str(text))})
    session["updated_at"] = now_iso()
    if session.get("title") in {None, "", "新对话"}:
        session["title"] = short_text(text, 18)
    session["preview"] = "正在处理…"


def apply_reply(
    session: dict[str, Any],
    reply: Any,
    assistant_html: str,
    *,
    trace_id: str = "",
) -> None:
    messages = session.setdefault("messages", [])
    messages.append({"role": "assistant", "content": assistant_html})
    session["reply"] = reply_to_dict(reply)
    session["outcome"] = str(field(reply, "outcome", "") or "")
    session["preview"] = session_preview(reply)
    session["updated_at"] = now_iso()
    visible_steps = visible_flow_step_count(reply)
    claims = list(field(reply, "claims", []) or [])
    session["active_tab"] = "checklist" if visible_steps or not claims else "citations"
    session["trace_id"] = trace_id
    session["duration_ms"] = reply_duration_ms(reply)


def apply_error(session: dict[str, Any], message: str) -> None:
    session.setdefault("messages", []).append(
        {
            "role": "assistant",
            "content": (
                '<span class="w8-badge w8-badge-abstain">'
                '<span class="w8-dot"></span>请求失败</span>\n\n' + html.escape(message)
            ),
        }
    )
    session["outcome"] = "GENERATION_FAILED"
    session["preview"] = "请求失败 · 可稍后再试"
    session["reply"] = None
    session["active_tab"] = "debug"
    session["updated_at"] = now_iso()


def user_turn_count(session: dict[str, Any]) -> int:
    return sum(1 for item in session.get("messages", []) if item.get("role") == "user")


def session_meta(session: dict[str, Any], *, profile: str = "") -> str:
    # 不展示模型名；profile 保留为调用兼容参数，不进入主界面。
    _ = profile
    turns = user_turn_count(session)
    duration = session.get("duration_ms")
    if duration:
        return f"第 {turns} 轮 · {duration / 1000:.1f}s（示例）"
    return f"第 {turns} 轮" if turns else "选择左侧历史，或直接提问"


def session_trace_id(session: dict[str, Any]) -> str:
    return str(session.get("trace_id") or session.get("thread_id", ""))[:16]


def reset_session(session: dict[str, Any]) -> None:
    session["messages"] = []
    session["reply"] = None
    session["outcome"] = ""
    session["title"] = "新对话"
    session["preview"] = "开始提问"
    session["active_tab"] = "checklist"
    session["updated_at"] = now_iso()
    session.pop("trace_id", None)
    session.pop("duration_ms", None)

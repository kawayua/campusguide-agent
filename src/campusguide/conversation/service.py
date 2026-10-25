"""多轮流程会话服务：匹配、槽位追问、更正与 closed 处理。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from campusguide.conversation.session import ConversationSession, SessionStore
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.models import (
    CalendarEvent,
    ChecklistItem,
    Citation,
    Claim,
    FlowDefinition,
)
from campusguide.domain.profiles import UserProfile
from campusguide.flows.engine import FlowEvaluation, evaluate_flow, render_checklist_text
from campusguide.flows.registry import FlowMatch, FlowRegistry, match_flows
from campusguide.flows.slots import extract_slots

Fallback = Callable[[str], str]


@dataclass
class ConversationReply:
    text: str
    flow_id: str | None = None
    status: str | None = None
    checklist: list[ChecklistItem] = field(default_factory=list)
    next_slot: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    ambiguous_flow_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    closed: bool = False
    # W6 图门面扩展
    claims: list[Claim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    outcome: str = ""
    decision: str = ""
    handoff: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    clarification: dict[str, Any] | None = None


def _default_fallback(message: str) -> str:
    return (
        "没有匹配到已登记的办事流程。可以换一种说法，或直接描述你要办的事项"
        "（例如“国家奖学金”“在读证明”“推免”）。"
    )


class ConversationService:
    def __init__(
        self,
        registry: FlowRegistry | None = None,
        *,
        calendar: Mapping[str, CalendarEvent] | None = None,
        fallback: Fallback | None = None,
        today_provider: Callable[[], date] | None = None,
        max_clarify: int = 3,
        graph_runner: Any | None = None,
    ) -> None:
        if registry is None and graph_runner is None:
            raise ValueError("必须提供 FlowRegistry 或 graph_runner")
        self._registry = registry
        self._calendar = dict(calendar or {})
        self._fallback = fallback or _default_fallback
        self._today_provider = today_provider or date.today
        self._max_clarify = max_clarify
        self._graph_runner = graph_runner
        self._store = SessionStore()

    @property
    def store(self) -> SessionStore:
        return self._store

    def _as_of(self, value: date | datetime | None) -> date | datetime:
        return value if value is not None else self._today_provider()

    @staticmethod
    def _as_date(value: date | datetime) -> date:
        return value.date() if isinstance(value, datetime) else value

    def _active_flow(
        self, session: ConversationSession, as_of: date | datetime
    ) -> FlowDefinition | None:
        if not session.active_flow_id:
            return None
        flow = self._registry.get_flow(session.active_flow_id, as_of=self._as_date(as_of))
        if flow is None:
            session.active_flow_id = None
            session.flow_version = None
        return flow

    def _reset_flow_state(self, session: ConversationSession) -> None:
        session.active_flow_id = None
        session.flow_version = None
        session.slots = {}
        session.declined_slots = []
        session.asked_slots = []
        session.pending_slot = None
        session.clarify_count = 0
        session.flow_instance = None
        session.pending_flow_ids = []

    def _start_flow(self, session: ConversationSession, flow: FlowDefinition) -> None:
        self._reset_flow_state(session)
        session.active_flow_id = flow.flow_id
        session.flow_version = flow.version
        session.pending_flow_ids = []

    def _evaluate(
        self,
        session: ConversationSession,
        flow: FlowDefinition,
        as_of: date | datetime,
    ) -> FlowEvaluation:
        return evaluate_flow(
            flow,
            session.slots,
            as_of=as_of,
            calendar=self._calendar,
            done_step_ids=set(session.flow_instance.done_step_ids if session.flow_instance else []),
            declined_slots=set(session.declined_slots),
            asked_slots=set(session.asked_slots),
        )

    def _closed_reply(self, evaluation: FlowEvaluation) -> ConversationReply:
        flow = evaluation.flow
        lines = [f"{flow.title}（{flow.batch_label or '当前批次'}）当前状态：已截止。"]
        labels = {
            "closed": "已截止",
            "available": "仍可办理",
            "conditional": "待确认",
            "blocked": "受阻",
            "done": "已完成",
        }
        for item in evaluation.checklist:
            if item.status == "not_applicable":
                continue
            label = labels.get(item.status, item.status)
            note = item.status_note or item.deadline_note or item.blocked_reason or ""
            if note:
                lines.append(f"- [{label}] {item.title}：{note}")
            else:
                lines.append(f"- [{label}] {item.title}")
            if item.description:
                lines.append(f"  {item.description}")
        lines.append("下一批次的时间、名额和材料以学校/学院当年通知为准；本流程不推断下一年日期。")
        if flow.handoff.department:
            lines.append(f"官方入口：{flow.handoff.department}")
            if flow.handoff.url:
                lines.append(str(flow.handoff.url))
            if flow.handoff.note:
                lines.append(flow.handoff.note)
        return ConversationReply(
            text="\n".join(lines),
            flow_id=flow.flow_id,
            status=evaluation.status,
            checklist=evaluation.checklist,
            missing_slots=evaluation.missing_slots,
            warnings=evaluation.warnings,
            closed=True,
        )

    def _full_reply(self, evaluation: FlowEvaluation) -> ConversationReply:
        flow = evaluation.flow
        lines = [render_checklist_text(evaluation)]
        if evaluation.eligible is False:
            lines.insert(0, "按已提供的信息，当前不满足前置条件；以下是依据和人工入口。")
        lines.append(f"人工入口：{flow.handoff.department}")
        if flow.handoff.url:
            lines.append(str(flow.handoff.url))
        if flow.handoff.note:
            lines.append(flow.handoff.note)
        return ConversationReply(
            text="\n".join(lines),
            flow_id=flow.flow_id,
            status=evaluation.status,
            checklist=evaluation.checklist,
            missing_slots=evaluation.missing_slots,
            warnings=evaluation.warnings,
        )

    def _ask_reply(
        self,
        evaluation: FlowEvaluation,
        session: ConversationSession,
        slot_name: str,
    ) -> ConversationReply:
        definition = next((slot for slot in evaluation.flow.slots if slot.name == slot_name), None)
        if definition is None:
            return self._full_reply(evaluation)
        session.pending_slot = definition.name
        if definition.name not in session.asked_slots:
            session.asked_slots.append(definition.name)
        text = definition.question
        if definition.why_needed:
            text += f"\n（需要它来判断：{definition.why_needed}）"
        text += "\n可以直接回答；不想说也可以说“跳过”。"
        return ConversationReply(
            text=text,
            flow_id=evaluation.flow.flow_id,
            status=evaluation.status,
            checklist=evaluation.checklist,
            next_slot=definition.name,
            missing_slots=evaluation.missing_slots,
            warnings=evaluation.warnings,
        )

    def _apply_extraction(
        self, session: ConversationSession, message: str, flow: FlowDefinition
    ) -> None:
        extraction = extract_slots(
            message,
            flow.slots,
            turn_id=session.turn_id,
            existing=session.slots,
            pending_slot=session.pending_slot,
            allow_overwrite=True,
        )
        if extraction.values:
            session.slots.update(extraction.values)
            session.clarify_count = 0
        if extraction.declined_slots:
            for name in extraction.declined_slots:
                if name not in session.declined_slots:
                    session.declined_slots.append(name)
        if session.pending_slot and (
            session.pending_slot in extraction.values
            or session.pending_slot in extraction.declined_slots
        ):
            session.pending_slot = None

    def _match(
        self,
        session: ConversationSession,
        message: str,
        as_of: date | datetime,
    ) -> list[FlowMatch]:
        flows = self._registry.active_flows(as_of=self._as_date(as_of))
        return match_flows(message, flows)

    def handle(
        self,
        session_id: str,
        message: str,
        *,
        as_of: date | datetime | None = None,
        audience: Audience | None = None,
        profile: UserProfile | None = None,
        block_types: list[BlockType] | None = None,
    ) -> ConversationReply:
        if self._graph_runner is not None:
            return self._handle_graph(
                session_id,
                message,
                as_of=as_of,
                audience=audience,
                profile=profile,
                block_types=block_types,
            )
        if self._registry is None:
            raise RuntimeError("legacy 模式缺少 FlowRegistry")
        session = self._store.get(session_id)
        session.turn_id += 1
        effective_as_of = self._as_of(as_of)
        text = message.strip()
        if not text:
            return ConversationReply(text="请描述你要办理的事项。")

        # 取消/重来
        if text in {"取消", "重来", "重新开始", "换个流程", "换一个"}:
            self._reset_flow_state(session)
            return ConversationReply(text="已退出当前流程。请告诉我要办理的事项。")

        # 歧义候选选择：数字或名称
        if session.pending_flow_ids:
            candidates = [
                flow
                for flow in self._registry.active_flows(as_of=self._as_date(effective_as_of))
                if flow.flow_id in session.pending_flow_ids
            ]
            selected: FlowDefinition | None = None
            if text in {"1", "一", "第一个"} and candidates:
                selected = candidates[0]
            elif text in {"2", "二", "第二个"} and len(candidates) > 1:
                selected = candidates[1]
            elif text in {"3", "三", "第三个"} and len(candidates) > 2:
                selected = candidates[2]
            else:
                exact = match_flows(text, candidates)
                if len(exact) == 1:
                    selected = exact[0].flow
            if selected is not None:
                self._start_flow(session, selected)
                self._apply_extraction(session, text, selected)
                return self._respond_with_flow(session, selected, effective_as_of)
            return ConversationReply(
                text="请回复序号或流程名："
                + "；".join(f"{index + 1}. {flow.title}" for index, flow in enumerate(candidates)),
                ambiguous_flow_ids=[flow.flow_id for flow in candidates],
            )

        flow = self._active_flow(session, effective_as_of)
        if flow is not None:
            self._apply_extraction(session, text, flow)
            return self._respond_with_flow(session, flow, effective_as_of)

        matches = self._match(session, text, effective_as_of)
        if len(matches) == 1:
            flow = matches[0].flow
            self._start_flow(session, flow)
            self._apply_extraction(session, text, flow)
            return self._respond_with_flow(session, flow, effective_as_of)
        if len(matches) > 1:
            session.pending_flow_ids = [item.flow.flow_id for item in matches]
            return ConversationReply(
                text="你要办理的是哪一项？请回复序号或流程名："
                + "；".join(
                    f"{index + 1}. {item.flow.title}" for index, item in enumerate(matches)
                ),
                ambiguous_flow_ids=[item.flow.flow_id for item in matches],
            )
        return ConversationReply(text=self._fallback(text))

    def _handle_graph(
        self,
        session_id: str,
        message: str,
        *,
        as_of: date | datetime | None,
        audience: Audience | None,
        profile: UserProfile | None,
        block_types: list[BlockType] | None,
    ) -> ConversationReply:
        text = message.strip()
        if not text:
            return ConversationReply(text="请描述你要办理的事项。")
        if text in {"取消", "重来", "重新开始", "换个流程", "换一个"}:
            reset = getattr(self._graph_runner, "reset", None)
            if callable(reset):
                reset(session_id)
            return ConversationReply(text="已退出当前流程。请告诉我要办理的事项。")
        effective = as_of if as_of is not None else self._today_provider()
        as_date = self._as_date(effective) if isinstance(effective, datetime) else effective
        state = self._graph_runner.invoke(
            session_id,
            text,
            as_of=as_date,
            audience=audience,
            profile=profile,
            block_types=block_types,
        )
        plan = state.get("plan")
        clarification = state.get("clarification")
        next_slot = None
        if clarification and clarification.get("reason") == "SLOT_MISSING":
            next_slot = state.get("pending_slot")
            if next_slot is None:
                missing = clarification.get("missing_slots") or []
                next_slot = missing[0] if missing else None
        outcome = state.get("outcome") or {}
        warnings = list(state.get("retrieval_warnings") or [])
        if plan is not None:
            warnings.extend(getattr(plan, "warnings", []) or [])
        return ConversationReply(
            text=state.get("final_answer", ""),
            flow_id=state.get("flow_id"),
            status=state.get("flow_status"),
            checklist=list(state.get("checklist") or []),
            next_slot=next_slot,
            missing_slots=list(state.get("missing_slots") or []),
            ambiguous_flow_ids=list(state.get("pending_flow_ids") or []),
            warnings=list(dict.fromkeys(warnings)),
            closed=bool(state.get("closed")),
            claims=list(state.get("claims") or []),
            citations=list(state.get("citations") or []),
            outcome=str(outcome.get("code") or ""),
            decision=(
                state.get("decision").value if getattr(state.get("decision"), "value", None) else ""
            ),
            handoff=dict(state.get("handoff") or {}),
            trace=list(state.get("trace") or []),
            clarification=dict(clarification) if clarification else None,
        )

    def reset(self, session_id: str) -> None:
        self._store.reset(session_id)
        reset = getattr(self._graph_runner, "reset", None)
        if callable(reset):
            reset(session_id)

    def _respond_with_flow(
        self,
        session: ConversationSession,
        flow: FlowDefinition,
        as_of: date | datetime,
    ) -> ConversationReply:
        evaluation = self._evaluate(session, flow, as_of)
        if evaluation.status == "closed":
            return self._closed_reply(evaluation)
        if evaluation.next_slot is None:
            return self._full_reply(evaluation)
        slot_name = evaluation.next_slot.name
        if slot_name in session.asked_slots:
            session.clarify_count += 1
            if session.clarify_count > self._max_clarify:
                lines = [
                    "连续几轮没有拿到足够信息，先给你通用路径，避免继续追问：",
                    render_checklist_text(evaluation),
                    f"人工入口：{flow.handoff.department}",
                ]
                if flow.handoff.url:
                    lines.append(str(flow.handoff.url))
                return ConversationReply(
                    text="\n".join(lines),
                    flow_id=flow.flow_id,
                    status=evaluation.status,
                    checklist=evaluation.checklist,
                    missing_slots=evaluation.missing_slots,
                    warnings=evaluation.warnings,
                )
        return self._ask_reply(evaluation, session, slot_name)

"""多轮会话状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from campusguide.domain.models import FlowInstance, SlotValue


@dataclass
class ConversationSession:
    session_id: str
    turn_id: int = 0
    active_flow_id: str | None = None
    flow_version: str | None = None
    slots: dict[str, SlotValue] = field(default_factory=dict)
    declined_slots: list[str] = field(default_factory=list)
    asked_slots: list[str] = field(default_factory=list)
    pending_slot: str | None = None
    clarify_count: int = 0
    flow_instance: FlowInstance | None = None
    pending_flow_ids: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.now)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}

    def get(self, session_id: str) -> ConversationSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = ConversationSession(session_id=session_id)
        return self._sessions[session_id]

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

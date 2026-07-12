"""Session-scoped chat agent registry and server-side route selection."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from agents.appointment_agent import AppointmentAgent
from agents.consultant_agent import ConsultantAgent
from agents.task_classification_agent import TaskClassificationAgent

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 60 * 60
MAX_CHAT_SESSIONS = 100
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass
class ChatSession:
    session_id: str
    task_agent: TaskClassificationAgent
    last_access: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatSessionRegistry:
    """Bounded in-memory registry that isolates Agent state by browser session."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS, max_sessions: int = MAX_CHAT_SESSIONS):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, ChatSession] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def normalize_session_id(session_id: Optional[str]) -> str:
        candidate = (session_id or "").strip()
        if candidate and _SESSION_ID_PATTERN.fullmatch(candidate):
            return candidate
        return str(uuid.uuid4())

    def get_or_create(self, session_id: Optional[str]) -> ChatSession:
        normalized = self.normalize_session_id(session_id)
        with self._lock:
            self._prune_locked()
            existing = self._sessions.pop(normalized, None)
            if existing is not None:
                existing.last_access = time.monotonic()
                self._sessions[normalized] = existing
                return existing

            session = ChatSession(
                session_id=normalized,
                task_agent=TaskClassificationAgent(
                    AppointmentAgent(session_id=normalized),
                    ConsultantAgent(session_id=normalized),
                ),
            )
            self._sessions[normalized] = session
            self._prune_locked()
            return session

    def get_existing(self, session_id: str) -> Optional[ChatSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def reset(self, session_id: Optional[str]) -> str:
        normalized = self.normalize_session_id(session_id)
        with self._lock:
            self._sessions.pop(normalized, None)
        logger.info("chat_session_reset session_id=%s", normalized)
        return str(uuid.uuid4())

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_access > self.ttl_seconds
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)


_chat_sessions = ChatSessionRegistry()


def get_chat_session_registry() -> ChatSessionRegistry:
    return _chat_sessions


def route_user_message(user_input: str) -> str:
    """High-precision backend pre-router; it never mutates Agent state."""
    normalized = "".join((user_input or "").lower().split())
    appointment_terms = (
        "预约",
        "预订",
        "我想约",
        "想约",
        "我要约",
        "帮我约",
        "约一下",
        "约一个",
        "安排",
        "指定发型师",
    )
    return "appointment" if any(term in normalized for term in appointment_terms) else "consultation"


async def ProcessUserInput_stream(
    user_input,
    state=None,
    context=None,
    session_id: Optional[str] = None,
    route: Optional[str] = None,
):
    """Stream a response through the Agent instance owned by one browser session."""
    del state, context
    session = _chat_sessions.get_or_create(session_id)
    try:
        async with session.lock:
            logger.info(
                "chat_request session_id=%s route=%s state=%s",
                session.session_id,
                route or "agent_classification",
                session.task_agent.state_manager.get_current_state().value,
            )
            if route in {"appointment", "consultation"}:
                stream = session.task_agent.route_task_stream(user_input, route)
            else:
                stream = session.task_agent.classify_task_stream(user_input)
            async for token in stream:
                yield token
    except Exception as exc:
        logger.exception("chat_processing_failed session_id=%s", session.session_id)
        yield f"[ERROR]聊天模型未正确配置或调用失败：{exc}\n"

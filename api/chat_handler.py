"""Session-scoped chat agent registry and server-side route selection."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from agents.appointment_agent import AppointmentAgent
from agents.appointment.availability_parser import (
    CONSULTATION,
    CREATE_BOOKING,
    SEARCH_AVAILABILITY,
    detect_message_intent,
)
from agents.appointment.lifecycle_parser import (
    LIFECYCLE_INTENTS,
    detect_lifecycle_intent,
)
from agents.consultant_agent import ConsultantAgent
from agents.task_classification_agent import TaskClassificationAgent
from config.model_provider import classify_chat_model_error, chat_model_user_message
from config.trace_context import get_trace_id

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 60 * 60
MAX_CHAT_SESSIONS = 100
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OWNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ACCOUNT_OWNER_PATTERN = re.compile(
    r"^account:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


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


def _normalized_message(user_input: str) -> str:
    normalized = "".join((user_input or "").lower().split())
    return re.sub(r"[，。！？,.!?]+$", "", normalized)


def resolve_owner_id(
    owner_id: Optional[str],
    session_id: str,
    *,
    authenticated: bool = False,
) -> str:
    """Validate the anonymous owner or temporarily fall back to the chat session."""
    candidate = (owner_id or "").strip()
    if authenticated:
        if _ACCOUNT_OWNER_PATTERN.fullmatch(candidate):
            return candidate
        raise ValueError("invalid_owner_id")
    if not candidate:
        logger.warning(
            "chat_owner_fallback_deprecated session_id=%s owner_id=%s",
            session_id,
            _identifier_log_value(session_id),
        )
        return session_id
    if candidate.lower().startswith("account:") or not _OWNER_ID_PATTERN.fullmatch(candidate):
        raise ValueError("invalid_owner_id")
    return candidate


def _identifier_log_value(value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"id-{digest}"


def has_pending_appointment_confirmation(session: Optional[ChatSession]) -> bool:
    """Read the AppointmentAgent's actual pending-confirmation state."""
    if session is None:
        return False
    appointment_agent = getattr(session.task_agent, "appointment_agent", None)
    history = getattr(appointment_agent, "appointment_history", {}) or {}
    return bool(history.get("awaiting_confirmation"))


def has_pending_availability_interaction(session: Optional[ChatSession]) -> bool:
    if session is None:
        return False
    appointment_agent = getattr(session.task_agent, "appointment_agent", None)
    history = getattr(appointment_agent, "appointment_history", {}) or {}
    return bool(history.get("awaiting_slot_selection") or history.get("awaiting_slot_confirmation"))


def has_active_availability_search(session: Optional[ChatSession]) -> bool:
    if session is None:
        return False
    appointment_agent = getattr(session.task_agent, "appointment_agent", None)
    history = getattr(appointment_agent, "appointment_history", {}) or {}
    return bool(history.get("availability_search_active"))


def has_partial_appointment_slots(session: Optional[ChatSession]) -> bool:
    if session is None:
        return False
    appointment_agent = getattr(session.task_agent, "appointment_agent", None)
    history = getattr(appointment_agent, "appointment_history", {}) or {}
    return any(
        history.get(key)
        for key in (
            "requested_date",
            "requested_exact_time",
            "requested_range_start",
            "project",
        )
    )


def has_active_lifecycle_interaction(session: Optional[ChatSession]) -> bool:
    if session is None:
        return False
    appointment_agent = getattr(session.task_agent, "appointment_agent", None)
    history = getattr(appointment_agent, "appointment_history", {}) or {}
    return any(
        history.get(key)
        for key in (
            "pending_lifecycle_action",
            "awaiting_lifecycle_selection",
            "awaiting_lifecycle_changes",
            "awaiting_lifecycle_confirmation",
        )
    )


def has_active_appointment_flow(session: Optional[ChatSession]) -> bool:
    if session is None:
        return False
    state_manager = getattr(session.task_agent, "state_manager", None)
    if state_manager is None or not hasattr(state_manager, "get_current_state"):
        return False
    current_state = state_manager.get_current_state()
    return getattr(current_state, "value", current_state) == "appointment"


def is_confirmation_response(user_input: str) -> bool:
    normalized = _normalized_message(user_input)
    return normalized in {
        "确认", "好的", "好", "可以", "是", "是的", "没问题", "同意",
        "就他", "就这个", "预约他", "取消", "不用了", "不确认", "换一个",
        "换其他发型师",
    }


def route_user_message(user_input: str, session: Optional[ChatSession] = None) -> str:
    """Select a route without mutating Agent state."""
    if has_active_lifecycle_interaction(session):
        return "appointment"
    lifecycle_intent = detect_lifecycle_intent(user_input)
    if lifecycle_intent in LIFECYCLE_INTENTS:
        return "appointment"
    if _normalized_message(user_input) in {
        "取消",
        "取消本次操作",
        "退出当前操作",
        "不用了",
        "先不预约了",
        "退出预约流程",
    }:
        return "appointment"
    if (
        has_pending_availability_interaction(session)
        or has_active_availability_search(session)
        or has_partial_appointment_slots(session)
    ):
        return "appointment"
    pending_confirmation = has_pending_appointment_confirmation(session)
    if pending_confirmation:
        if is_confirmation_response(user_input):
            return "appointment"
    elif has_active_appointment_flow(session):
        return "appointment"

    intent = detect_message_intent(user_input)
    if intent in {CREATE_BOOKING, SEARCH_AVAILABILITY}:
        return "appointment"
    if intent == CONSULTATION:
        return "consultation"
    return "agent"


async def ProcessUserInput_stream(
    user_input,
    state=None,
    context=None,
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    owner_authenticated: bool = False,
    route: Optional[str] = None,
):
    """Stream a response through the Agent instance owned by one browser session."""
    del state, context
    trace_id = get_trace_id(default=uuid.uuid4().hex)
    session = None
    try:
        session = _chat_sessions.get_or_create(session_id)
        async with session.lock:
            resolved_owner_id = resolve_owner_id(
                owner_id,
                session.session_id,
                authenticated=owner_authenticated,
            )
            lifecycle_intent = detect_lifecycle_intent(user_input)
            detected_intent = lifecycle_intent or detect_message_intent(user_input)
            effective_route = route_user_message(user_input, session)
            logger.info(
                "chat_route session_id=%s owner_id=%s user_message_intent=%s requested_route=%s "
                "effective_route=%s availability_search_active=%s lifecycle_active=%s "
                "pending_confirmation=%s state=%s",
                session.session_id,
                _identifier_log_value(resolved_owner_id),
                detected_intent,
                route or "unspecified",
                effective_route,
                has_active_availability_search(session),
                has_active_lifecycle_interaction(session),
                has_pending_appointment_confirmation(session),
                session.task_agent.state_manager.get_current_state().value,
            )
            if effective_route in {"appointment", "consultation"}:
                stream = session.task_agent.route_task_stream(
                    user_input,
                    effective_route,
                    owner_id=resolved_owner_id,
                )
            else:
                stream = session.task_agent.classify_task_stream(
                    user_input,
                    owner_id=resolved_owner_id,
                )
            emitted = False
            async for token in stream:
                if token is None:
                    continue
                token_text = str(token)
                if not token_text:
                    continue
                emitted = True
                yield token_text
            if not emitted:
                logger.warning(
                    "chat_empty_stream trace_id=%s session_id=%s effective_route=%s",
                    trace_id,
                    session.session_id,
                    effective_route,
                )
                yield "[REPLY][系统]服务未返回有效内容，请稍后重试。"
    except ValueError as exc:
        resolved_session_id = session.session_id if session else "uninitialized"
        if str(exc) == "invalid_owner_id":
            logger.warning(
                "chat_owner_rejected trace_id=%s session_id=%s owner_id=invalid",
                trace_id,
                resolved_session_id,
            )
            yield "[REPLY][系统]客户端预约标识无效，请刷新页面后重试。"
        else:
            reason = classify_chat_model_error(exc)
            logger.warning(
                "chat_processing_failed trace_id=%s session_id=%s llm_status=%s "
                "exception_type=%s",
                trace_id,
                resolved_session_id,
                reason,
                type(exc).__name__,
                exc_info=reason == "unexpected_error",
            )
            yield f"[REPLY][系统]{chat_model_user_message(reason)}"
    except Exception as exc:
        reason = classify_chat_model_error(exc)
        resolved_session_id = session.session_id if session else "uninitialized"
        logger.warning(
            "chat_processing_failed trace_id=%s session_id=%s llm_status=%s "
            "exception_type=%s",
            trace_id,
            resolved_session_id,
            reason,
            type(exc).__name__,
            exc_info=reason == "unexpected_error",
        )
        yield f"[REPLY][系统]{chat_model_user_message(reason)}"

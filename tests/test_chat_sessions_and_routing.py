import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.appointment.appointment_processor import AppointmentProcessor
from agents.appointment.message_builder import MessageBuilder
from api import chat_handler
from config.constants import StateEnum
from web.routes import router as web_router


def test_backend_pre_router_prioritizes_booking_intent():
    assert chat_handler.route_user_message("我想预约男士短发，后天下午两点") == "appointment"
    assert chat_handler.route_user_message("我要预约林浩2026年7月15日下午两点做男士短发") == "appointment"
    assert chat_handler.route_user_message("帮我预订一个剪发服务，可以吗？") == "appointment"
    assert chat_handler.route_user_message("染发后如何护理？") == "consultation"


def test_page_uses_backend_router_and_session_aware_reset():
    html = Path("web/templates/index.html").read_text(encoding="utf-8")

    assert "shouldUseConsultationApi" not in html
    assert "'/api/chat/route'" in html
    assert "'/api/chat/reset'" in html
    assert "session_id: sessionId" in html


def test_route_and_reset_endpoints_are_side_effect_free(monkeypatch):
    class FakeRegistry:
        def __init__(self):
            self.reset_value = None

        def reset(self, session_id):
            self.reset_value = session_id
            return "new-session"

    registry = FakeRegistry()
    monkeypatch.setattr("web.routes.get_chat_session_registry", lambda: registry)
    app = FastAPI()
    app.include_router(web_router)

    with TestClient(app) as client:
        routed = client.post("/api/chat/route", json={"message": "我想预约男士短发，后天下午两点"})
        reset = client.post("/api/chat/reset", json={"session_id": "session-a"})

    assert routed.json() == {"route": "appointment"}
    assert reset.json() == {"status": "reset", "session_id": "new-session"}
    assert registry.reset_value == "session-a"


def test_session_registry_isolates_and_resets_agent_state(monkeypatch):
    class FakeAppointmentAgent:
        def __init__(self, session_id):
            self.session_id = session_id
            self.appointment_history = {}
            self.chat_history = []

    class FakeConsultantAgent:
        def __init__(self, session_id):
            self.session_id = session_id

    class FakeTaskAgent:
        def __init__(self, appointment_agent, consultant_agent):
            self.appointment_agent = appointment_agent
            self.consultant_agent = consultant_agent
            self.state_manager = SimpleNamespace(state=SimpleNamespace(value=StateEnum.CLASSIFY))

    monkeypatch.setattr(chat_handler, "AppointmentAgent", FakeAppointmentAgent)
    monkeypatch.setattr(chat_handler, "ConsultantAgent", FakeConsultantAgent)
    monkeypatch.setattr(chat_handler, "TaskClassificationAgent", FakeTaskAgent)
    registry = chat_handler.ChatSessionRegistry(ttl_seconds=3600, max_sessions=10)

    session_a = registry.get_or_create("session-a")
    session_a.task_agent.appointment_agent.appointment_history["awaiting_confirmation"] = True
    session_a.task_agent.appointment_agent.chat_history.append("pending")
    session_a.task_agent.state_manager.state.value = StateEnum.APPOINTMENT

    session_b = registry.get_or_create("session-b")
    assert session_b.task_agent.appointment_agent.appointment_history == {}
    assert session_b.task_agent.state_manager.state.value == StateEnum.CLASSIFY

    new_session_id = registry.reset("session-a")
    assert registry.get_existing("session-a") is None
    replacement = registry.get_or_create(new_session_id)
    assert replacement.task_agent.appointment_agent.appointment_history == {}
    assert replacement.task_agent.appointment_agent.chat_history == []
    assert replacement.task_agent.state_manager.state.value == StateEnum.CLASSIFY


def test_new_complete_request_replaces_pending_recommendation():
    processor = AppointmentProcessor(None, None, None, None)
    history = {
        "start_time": "2026-07-14 10:00",
        "project": "男士短发",
        "duration": "45分钟",
        "stylist_name": "陈宇",
        "awaiting_confirmation": True,
        "recommended_stylist": {"id": 2, "name": "陈宇"},
        "original_stylist": {"id": 1, "name": "林浩"},
        "confirmed_stylist": {"id": 2, "name": "陈宇"},
        "recommendation_declined": True,
    }
    data = {
        "start_time": "2026-07-15 14:00",
        "project": "男士短发",
        "duration": "未知",
        "stylist_name": "林浩",
        "confirmation": "未知",
    }

    finished = processor.update_history_from_data(history, data)

    assert finished is True
    assert history["start_time"] == "2026-07-15 14:00"
    assert history["duration"] == "45分钟"
    assert history["stylist_name"] == "林浩"
    for field in (
        "awaiting_confirmation",
        "recommended_stylist",
        "original_stylist",
        "confirmed_stylist",
        "recommendation_declined",
    ):
        assert field not in history


def test_empty_missing_list_never_renders_empty_missing_message():
    processor = AppointmentProcessor(None, None, None, None)
    history = {
        "start_time": "2026-07-15 14:00",
        "project": "男士短发",
        "duration": "45分钟",
        "awaiting_confirmation": True,
    }

    async def collect():
        return "".join([
            token
            async for token in processor.handle_incomplete_info(
                {}, history, session_id="session-a", current_state=StateEnum.APPOINTMENT
            )
        ])

    message = asyncio.run(collect())
    assert "缺少：" not in message
    assert "明确回复" in message


def test_chat_success_message_includes_persisted_appointment_details():
    message = MessageBuilder().create_appointment_success_message(
        {"id": 1, "name": "林浩"},
        {
            "appointment_id": 123456,
            "start_time": "2026-07-15 14:00",
            "project": "男士短发",
            "duration": "45分钟",
            "price": 88,
        },
    )

    assert "预约编号：123456" in message
    assert "2026年7月15日14:00" in message
    assert "林浩" in message
    assert "男士短发" in message
    assert "45分钟" in message
    assert "88元" in message

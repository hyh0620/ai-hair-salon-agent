import asyncio
from datetime import date, datetime
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import (
    AppointmentProcessor,
    WeatherContextResult,
)
from agents.appointment.input_parser import InputParser
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from agents.appointment_agent import AppointmentAgent
from api import chat_handler
from config.constants import StateEnum
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService
from services.user_behavior_service import UserBehaviorService
from web.routes import router as web_router


def test_backend_pre_router_prioritizes_booking_intent():
    assert chat_handler.route_user_message("我想预约男士短发，后天下午两点") == "appointment"
    assert chat_handler.route_user_message("我要预约林浩2026年7月15日下午两点做男士短发") == "appointment"
    assert chat_handler.route_user_message("帮我预订一个剪发服务，可以吗？") == "appointment"
    assert chat_handler.route_user_message("染发后如何护理？") == "consultation"


def fake_chat_session(session_id="session-a", *, pending=False, state=StateEnum.CLASSIFY):
    appointment_agent = SimpleNamespace(
        appointment_history={"awaiting_confirmation": True} if pending else {},
    )
    state_manager = SimpleNamespace(get_current_state=lambda: state)
    task_agent = SimpleNamespace(
        appointment_agent=appointment_agent,
        state_manager=state_manager,
    )
    return chat_handler.ChatSession(session_id=session_id, task_agent=task_agent)


@pytest.mark.parametrize("message", ["确认", "好的", "取消"])
def test_pending_confirmation_routes_confirmation_language_to_appointment(message):
    session = fake_chat_session(pending=True, state=StateEnum.APPOINTMENT)

    assert chat_handler.route_user_message(message, session) == "appointment"


def test_confirmation_without_pending_state_does_not_route_to_appointment():
    assert chat_handler.route_user_message("确认", fake_chat_session()) == "consultation"


def test_pending_confirmation_is_isolated_by_session():
    session_a = fake_chat_session("session-a", pending=True, state=StateEnum.APPOINTMENT)
    session_b = fake_chat_session("session-b", pending=False, state=StateEnum.CLASSIFY)

    assert chat_handler.route_user_message("确认", session_a) == "appointment"
    assert chat_handler.route_user_message("确认", session_b) == "consultation"


def test_stream_backend_overrides_stale_consultation_route(monkeypatch):
    session = fake_chat_session(pending=True, state=StateEnum.APPOINTMENT)

    async def route_task_stream(message, route):
        session.task_agent.effective_route = route
        yield route

    session.task_agent.route_task_stream = route_task_stream
    registry = SimpleNamespace(get_or_create=lambda session_id: session)
    monkeypatch.setattr(chat_handler, "_chat_sessions", registry)

    async def collect():
        return "".join([
            token
            async for token in chat_handler.ProcessUserInput_stream(
                "确认",
                session_id="session-a",
                route="consultation",
            )
        ])

    assert asyncio.run(collect()) == "appointment"
    assert session.task_agent.effective_route == "appointment"


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
            self.session = fake_chat_session(pending=True, state=StateEnum.APPOINTMENT)

        def reset(self, session_id):
            self.reset_value = session_id
            return "new-session"

        def get_existing(self, session_id):
            assert session_id == "session-a"
            return self.session

    registry = FakeRegistry()
    monkeypatch.setattr("web.routes.get_chat_session_registry", lambda: registry)
    app = FastAPI()
    app.include_router(web_router)

    with TestClient(app) as client:
        routed = client.post(
            "/api/chat/route",
            json={"message": "确认", "session_id": "session-a"},
        )
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


def build_confirmation_processor(tmp_path):
    db_path = f"sqlite:///{tmp_path / 'confirmation.db'}"
    StylistService(db_path).initialize_default_stylists()
    appointment_service = AppointmentService(db_path)
    processor = AppointmentProcessor(
        input_parser=InputParser.__new__(InputParser),
        stylist_finder=StylistFinder(appointment_service),
        message_builder=MessageBuilder(),
        appointment_database=AppointmentDatabase(
            appointment_service=appointment_service,
            user_behavior_service=UserBehaviorService(db_path),
        ),
        llm=None,
    )
    return processor, appointment_service


def pending_history(recommended_stylist):
    return {
        "start_time": "2026-07-16 15:00",
        "project": "男士短发",
        "duration": "45分钟",
        "stylist_name": "周晴",
        "awaiting_confirmation": True,
        "recommended_stylist": recommended_stylist,
        "original_stylist": {"id": 4, "name": "周晴"},
    }


def test_confirmation_saves_real_id_before_weather(monkeypatch, tmp_path):
    monkeypatch.setattr(
        time_config,
        "now",
        lambda: datetime(2026, 7, 15, 12, 0, tzinfo=time_config.BEIJING_TZ),
    )
    processor, appointment_service = build_confirmation_processor(tmp_path)
    recommended = appointment_service.get_stylist_by_name("林浩")
    history = pending_history(recommended)
    events = []

    class RecordingWeather:
        async def get_weather_context(self, appointment_time=None):
            schedules = appointment_service.get_stylist_schedules(recommended["id"], date(2026, 7, 16))
            assert schedules
            assert schedules[0]["appointment_id"] == history["appointment_id"]
            events.append(("weather_after_save", history["appointment_id"]))
            return WeatherContextResult(
                status="available",
                context="天气提醒：预计预约时段上海晴。",
                precipitation_probability=0,
                precipitation=0,
                weather_code=0,
            )

    processor.weather_tool = RecordingWeather()

    class ForbiddenInputParser:
        def parse_stream(self, *args, **kwargs):
            raise AssertionError("explicit confirmation must not call the LLM parser")

    agent = AppointmentAgent.__new__(AppointmentAgent)
    agent.session_id = "confirmation-session"
    agent.appointment_history = history
    agent.appointment_processor = processor
    agent.input_parser = ForbiddenInputParser()
    agent.message_builder = processor.message_builder
    agent.chat_history = []
    agent.state = SimpleNamespace(value=StateEnum.APPOINTMENT)
    agent.finished = False

    async def collect():
        return "".join([
            token
            async for token in agent.run_stream("确认")
        ])

    reply = asyncio.run(collect())
    match = re.search(r"预约编号：(\d+)", reply)
    assert match
    assert int(match.group(1)) == history["appointment_id"]
    assert "已为您预约林浩" in reply
    assert "天气提醒" in reply
    assert events == [("weather_after_save", int(match.group(1)))]


def test_confirmation_cancellation_does_not_save_or_call_weather(tmp_path):
    processor, appointment_service = build_confirmation_processor(tmp_path)
    recommended = appointment_service.get_stylist_by_name("林浩")
    history = pending_history(recommended)

    class ForbiddenWeather:
        async def get_weather_context(self, appointment_time=None):
            raise AssertionError("weather must not run for a cancelled confirmation")

    processor.weather_tool = ForbiddenWeather()
    assert processor.update_history_from_data(history, {"confirmation": "取消"}) is True

    async def collect():
        return "".join([
            token
            async for token in processor.handle_complete_appointment(history, "cancel-session")
        ])

    reply = asyncio.run(collect())
    assert "理解" in reply
    assert appointment_service.get_stylist_schedules(recommended["id"], date(2026, 7, 16)) == []

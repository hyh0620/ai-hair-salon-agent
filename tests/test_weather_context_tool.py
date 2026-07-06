import asyncio

import pytest
from fastapi.testclient import TestClient

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import AppointmentProcessor, WeatherTool
from agents.appointment.input_parser import InputParser
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from app import create_app
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService
from services.user_behavior_service import UserBehaviorService


def run(coro):
    return asyncio.run(coro)


def build_processor(tmp_path):
    db_path = f"sqlite:///{tmp_path / 'salon.db'}"
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
    stylist = appointment_service.get_stylist_by_name("林浩")
    return processor, stylist


def appointment_history(start_time="2026-07-13 10:00", project="男士短发"):
    return {
        "project": project,
        "start_time": start_time,
        "duration": "45分钟",
        "style_preference": "渐变推剪",
        "budget": "100元",
    }


def forbid_http(monkeypatch):
    import aiohttp

    class ForbiddenSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("weather HTTP should not be called")

    monkeypatch.setattr(aiohttp, "ClientSession", ForbiddenSession)


def test_weather_disabled_does_not_call_http_and_booking_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")
    forbid_http(monkeypatch)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather_disabled"))

    assert "预约成功" in reply
    assert "当前天气" not in reply
    assert history["weather_status"] == "omitted"
    assert history["weather_unavailable_reason"] == "disabled"


def test_missing_openweather_key_does_not_call_http_or_fake_weather(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")
    forbid_http(monkeypatch)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "missing_key"))

    assert "预约成功" in reply
    assert "当前天气" not in reply
    assert "晴朗" not in reply
    assert history["weather_status"] == "omitted"
    assert history["weather_unavailable_reason"] == "missing_api_key"


def test_missing_weather_location_does_not_call_http(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.delenv("WEATHER_LOCATION", raising=False)
    forbid_http(monkeypatch)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "missing_location"))

    assert "预约成功" in reply
    assert "当前天气" not in reply
    assert history["weather_status"] == "omitted"
    assert history["weather_unavailable_reason"] == "missing_location"


def test_mock_openweather_200_appends_real_weather_context(monkeypatch, tmp_path):
    import aiohttp

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {
                "main": {"temp": 29.5, "feels_like": 31, "humidity": 70},
                "weather": [{"description": "小雨"}],
                "wind": {"speed": 3.2},
            }

    class FakeSession:
        calls = []
        timeout = None

        def __init__(self, *args, **kwargs):
            FakeSession.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params):
            FakeSession.calls.append((url, params))
            return FakeResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history(project="染发")

    reply = run(processor._process_successful_appointment(stylist, history, "weather_success"))

    assert "预约成功" in reply
    assert "Shanghai当前天气：小雨" in reply
    assert "染发或烫发后建议避免淋雨" in reply
    assert history["weather_status"] == "available"
    assert FakeSession.calls
    assert FakeSession.calls[0][1]["q"] == "Shanghai"
    assert FakeSession.timeout is not None


def test_weather_timeout_does_not_affect_booking(monkeypatch, tmp_path):
    import aiohttp

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")

    class TimeoutSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params):
            raise asyncio.TimeoutError()

    monkeypatch.setattr(aiohttp, "ClientSession", TimeoutSession)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather_timeout"))

    assert "预约成功" in reply
    assert "当前天气" not in reply
    assert history["weather_status"] == "unavailable"
    assert history["weather_unavailable_reason"] == "timeout"


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_weather_http_errors_do_not_fake_weather_or_fail_booking(monkeypatch, tmp_path, status_code):
    import aiohttp

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")

    class ErrorResponse:
        status = status_code

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class ErrorSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params):
            return ErrorResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", ErrorSession)
    processor, stylist = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, f"weather_http_{status_code}"))

    assert "预约成功" in reply
    assert "当前天气" not in reply
    assert history["weather_status"] == "unavailable"
    assert history["weather_unavailable_reason"] == f"http_{status_code}"


def test_structured_conflict_api_does_not_call_weather(monkeypatch, tmp_path):
    async def forbidden_weather_call(self):
        raise AssertionError("structured appointment API must not call weather")

    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_LOCATION", "Shanghai")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_conflict.db'}")
    monkeypatch.setattr(WeatherTool, "get_weather_context", forbidden_weather_call)

    payload = {
        "user_id": "weather_conflict_user",
        "project": "男士短发",
        "start_time": "2026-07-13 14:00",
        "duration": "45分钟",
        "stylist_id": 1,
        "style_preference": "渐变推剪",
    }

    with TestClient(create_app()) as client:
        first = client.post("/api/appointment/create", json=payload)
        second = client.post(
            "/api/appointment/create",
            json=payload | {"user_id": "weather_conflict_user_2"},
        )

    assert first.status_code == 200
    assert second.status_code == 409

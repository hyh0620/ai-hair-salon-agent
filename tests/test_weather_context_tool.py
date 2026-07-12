import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import AppointmentProcessor, WeatherTool
from agents.appointment.input_parser import InputParser
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService
from services.user_behavior_service import UserBehaviorService


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI_TZ)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def weather_environment(monkeypatch):
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setenv("WEATHER_LOCATION_NAME", "上海")
    monkeypatch.setenv("WEATHER_LATITUDE", "31.2304")
    monkeypatch.setenv("WEATHER_LONGITUDE", "121.4737")
    monkeypatch.setenv("WEATHER_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("WEATHER_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("WEATHER_FORECAST_DAYS", "16")
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    monkeypatch.setattr(WeatherTool, "_now", lambda self: FIXED_NOW)


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
    return processor, stylist, appointment_service


def appointment_history(start_time="2026-07-14 15:30", project="染发"):
    return {
        "project": project,
        "start_time": start_time,
        "duration": "150分钟",
        "style_preference": "自然",
        "budget": "500元",
    }


def hourly_payload():
    return {
        "hourly": {
            "time": [
                "2026-07-14T14:00",
                "2026-07-14T15:00",
                "2026-07-14T16:00",
                "2026-07-15T15:00",
            ],
            "temperature_2m": [27, 28, 29, 30],
            "apparent_temperature": [28, 30, 31, 32],
            "relative_humidity_2m": [65, 75, 78, 70],
            "precipitation_probability": [10, 40, 60, 20],
            "precipitation": [0, 0.4, 1.2, 0],
            "weather_code": [1, 2, 61, 0],
            "wind_speed_10m": [8, 12, 15, 10],
        }
    }


def install_fake_http(monkeypatch, *, payload=None, status=200, json_error=None, request_error=None):
    import aiohttp

    class FakeResponse:
        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            if json_error:
                raise json_error
            return payload

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
            if request_error:
                raise request_error
            return FakeResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return FakeSession


def forbid_http(monkeypatch):
    import aiohttp

    class ForbiddenSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("weather HTTP should not be called")

    monkeypatch.setattr(aiohttp, "ClientSession", ForbiddenSession)


def test_default_provider_is_open_meteo_shanghai(monkeypatch):
    for key in (
        "WEATHER_PROVIDER",
        "WEATHER_LOCATION_NAME",
        "WEATHER_LATITUDE",
        "WEATHER_LONGITUDE",
        "WEATHER_TIMEZONE",
    ):
        monkeypatch.delenv(key, raising=False)

    tool = WeatherTool()

    assert tool.provider == "open_meteo"
    assert tool.location_name == "上海"
    assert tool._latitude == 31.2304
    assert tool._longitude == 121.4737
    assert tool._timezone_name == "Asia/Shanghai"
    assert tool.is_configured


def test_open_meteo_needs_no_api_key_and_uses_expected_parameters(monkeypatch):
    fake_session = install_fake_http(monkeypatch, payload=hourly_payload())

    result = run(WeatherTool().get_weather_context("2026-07-14 15:30"))

    assert result.status == "available"
    assert result.forecast_time == datetime(2026, 7, 14, 15, 0, tzinfo=SHANGHAI_TZ)
    assert fake_session.calls
    url, params = fake_session.calls[0]
    assert url == "https://api.open-meteo.com/v1/forecast"
    assert params["latitude"] == 31.2304
    assert params["longitude"] == 121.4737
    assert params["timezone"] == "Asia/Shanghai"
    assert params["forecast_days"] == 16
    assert "precipitation_probability" in params["hourly"]
    assert "appid" not in params


def test_chinese_forecast_and_rain_aware_color_care(monkeypatch, tmp_path):
    install_fake_http(monkeypatch, payload=hourly_payload())
    processor, stylist, _ = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather-success"))

    assert "预约成功" in reply
    assert "天气提醒：预计预约时段上海多云" in reply
    for term in ("气温28°C", "体感30°C", "降水概率40%", "湿度75%", "风速12km/h"):
        assert term in reply
    assert "避免淋雨" in reply
    assert "雨具" in reply
    assert history["weather_status"] == "available"


def test_clear_weather_does_not_force_umbrella_advice(monkeypatch):
    payload = hourly_payload()
    payload["hourly"]["precipitation_probability"][1] = 0
    payload["hourly"]["precipitation"][1] = 0
    payload["hourly"]["weather_code"][1] = 0
    install_fake_http(monkeypatch, payload=payload)
    result = run(WeatherTool().get_weather_context("2026-07-14 15:00"))

    tip = AppointmentProcessor._weather_care_tip("染发", result)

    assert "雨具" not in tip
    assert "染烫后护理" in tip


def test_outside_forecast_horizon_is_omitted(monkeypatch):
    install_fake_http(monkeypatch, payload=hourly_payload())

    result = run(WeatherTool().get_weather_context("2026-07-20 15:00"))

    assert result.status == "omitted"
    assert result.reason == "outside_forecast_horizon"
    assert result.context == ""


@pytest.mark.parametrize(
    ("appointment_time", "reason"),
    [
        ("not-a-date", "invalid_appointment_time"),
        ("2026-07-12 15:00", "past_appointment"),
    ],
)
def test_invalid_or_past_appointment_is_omitted_without_http(monkeypatch, appointment_time, reason):
    forbid_http(monkeypatch)

    result = run(WeatherTool().get_weather_context(appointment_time))

    assert result.status == "omitted"
    assert result.reason == reason


def test_weather_disabled_does_not_call_http_and_booking_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    forbid_http(monkeypatch)
    processor, stylist, _ = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather-disabled"))

    assert "预约成功" in reply
    assert "天气提醒" not in reply
    assert history["weather_status"] == "omitted"
    assert history["weather_unavailable_reason"] == "disabled"


def test_timeout_does_not_affect_booking(monkeypatch, tmp_path):
    install_fake_http(monkeypatch, request_error=asyncio.TimeoutError())
    processor, stylist, _ = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather-timeout"))

    assert "预约成功" in reply
    assert "天气提醒" not in reply
    assert history["weather_status"] == "unavailable"
    assert history["weather_unavailable_reason"] == "timeout"


@pytest.mark.parametrize(("status_code", "reason"), [(400, "http_4xx"), (429, "http_4xx"), (500, "http_5xx")])
def test_http_errors_do_not_fake_weather(monkeypatch, tmp_path, status_code, reason):
    install_fake_http(monkeypatch, status=status_code)
    processor, stylist, _ = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, f"weather-http-{status_code}"))

    assert "预约成功" in reply
    assert "天气提醒" not in reply
    assert history["weather_status"] == "unavailable"
    assert history["weather_unavailable_reason"] == reason


@pytest.mark.parametrize(
    "payload,json_error",
    [
        ({"hourly": {"time": ["2026-07-14T15:00"]}}, None),
        (None, ValueError("invalid json")),
    ],
)
def test_malformed_response_does_not_affect_booking(monkeypatch, tmp_path, payload, json_error):
    install_fake_http(monkeypatch, payload=payload, json_error=json_error)
    processor, stylist, _ = build_processor(tmp_path)
    history = appointment_history()

    reply = run(processor._process_successful_appointment(stylist, history, "weather-malformed"))

    assert "预约成功" in reply
    assert "天气提醒" not in reply
    assert history["weather_status"] == "unavailable"
    assert history["weather_unavailable_reason"] == "malformed_response"


def test_save_conflict_does_not_call_weather(monkeypatch, tmp_path):
    processor, stylist, appointment_service = build_processor(tmp_path)
    history = appointment_history()
    details = appointment_service.build_appointment_details(history)
    start_time, end_time, _ = processor.stylist_finder.parse_time_and_duration(
        details["start_time"], details["duration"]
    )
    assert appointment_service.save_appointment(
        str(stylist["id"]), start_time, end_time, details, "existing"
    )

    async def forbidden_weather_call(self, appointment_time=None):
        raise AssertionError("weather must not run after a failed save")

    monkeypatch.setattr(WeatherTool, "get_weather_context", forbidden_weather_call)
    reply = run(processor._process_successful_appointment(stylist, appointment_history(), "conflict"))

    assert "预约保存失败" in reply
    assert "天气提醒" not in reply

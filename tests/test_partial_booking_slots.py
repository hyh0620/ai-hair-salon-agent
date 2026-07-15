import asyncio
from datetime import date, datetime
import json
import re
from types import SimpleNamespace

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import AppointmentProcessor, WeatherContextResult
from agents.appointment.input_parser import InputParser
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from agents.appointment_agent import AppointmentAgent
from config.constants import StateEnum
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.user_behavior_service import UserBehaviorService


FIXED_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=time_config.BEIJING_TZ)


class FakeBookingParser:
    """Simulate untrusted LLM output, including invented times."""

    def parse_stream(self, user_input, chat_history):
        payload = {
            "project": "未知",
            "start_time": "未知",
            "duration": "未知",
            "stylist_name": "未知",
            "confirmation": "未知",
            "unrelated": False,
        }
        if "男士短发" in user_input:
            payload["project"] = "男士短发"
            payload["start_time"] = "2026-07-17 15:00"
        elif "预约明天" in user_input and "两点" not in user_input and "下午" not in user_input:
            payload["start_time"] = "2026-07-17 00:00"
        elif "下午两点" in user_input:
            payload["start_time"] = "2026-07-16 14:00"
        elif "下午三点" in user_input:
            payload["start_time"] = "2026-07-16 15:00"
        elif "凌晨零点" in user_input:
            payload["start_time"] = "2026-07-17 00:00"
        elif "下午" in user_input:
            payload["start_time"] = "2026-07-17 12:00"
        yield json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def parse_data(content):
        return json.loads(content)


def build_partial_booking_agent(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'partial-booking.db'}"
    appointment_service = AppointmentService(db_url)
    stylist_id = appointment_service.add_stylist(
        "林浩", "男", "男士短发、渐变推剪、商务短发"
    )
    finder = StylistFinder(appointment_service)
    processor = AppointmentProcessor(
        input_parser=SimpleNamespace(),
        stylist_finder=finder,
        message_builder=MessageBuilder(),
        appointment_database=AppointmentDatabase(
            appointment_service=appointment_service,
            user_behavior_service=UserBehaviorService(db_url),
        ),
        llm=None,
    )
    agent = AppointmentAgent.__new__(AppointmentAgent)
    agent.session_id = "partial-booking-session"
    agent.unrelated_callback = None
    agent.state = SimpleNamespace(value=StateEnum.APPOINTMENT)
    agent.input_parser = FakeBookingParser()
    agent.stylist_finder = finder
    agent.message_builder = processor.message_builder
    agent.appointment_database = processor.appointment_database
    agent.appointment_processor = processor
    agent.chat_history = []
    agent.chats_by_session_id = {}
    agent.reset()
    return agent, appointment_service, stylist_id


async def ask(agent, message):
    return "".join([token async for token in agent.run_stream(message)])


def schedules_for(appointment_service, stylist_id):
    return appointment_service.get_stylist_schedules(stylist_id, date(2026, 7, 17))


def test_date_then_service_then_exact_time_saves_only_on_third_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)
    finder_calls = []
    weather_calls = []
    original_find = agent.stylist_finder.find_stylist_with_thought

    def tracked_find(*args, **kwargs):
        finder_calls.append("find")
        return original_find(*args, **kwargs)

    class RecordingWeather:
        async def get_weather_context(self, appointment_time=None):
            persisted = schedules_for(appointment_service, stylist_id)
            assert len(persisted) == 1
            weather_calls.append(persisted[0]["appointment_id"])
            return WeatherContextResult(status="available", context="天气提醒：预计预约时段上海晴。")

    agent.stylist_finder.find_stylist_with_thought = tracked_find
    agent.appointment_processor.weather_tool = RecordingWeather()

    first = asyncio.run(ask(agent, "预约明天"))
    assert agent.appointment_history["requested_date"] == "2026-07-17"
    assert agent.appointment_history.get("requested_exact_time") is None
    assert agent.appointment_history.get("start_time") is None
    assert "服务项目、预约时间" in first
    assert "已记录预约日期：明天" in first
    assert "预计需要多长时间" not in first
    assert finder_calls == []
    assert schedules_for(appointment_service, stylist_id) == []
    assert weather_calls == []

    second = asyncio.run(ask(agent, "男士短发"))
    assert agent.appointment_history["project"] == "男士短发"
    assert agent.appointment_history["duration"] == "45分钟"
    assert agent.appointment_history["price"] == 88
    assert agent.appointment_history.get("start_time") is None
    assert "门店标准时长为45分钟，价格88元" in second
    assert "明天希望预约上午、下午、晚上，还是具体几点" in second
    assert "预计需要多长时间" not in second
    assert finder_calls == []
    assert schedules_for(appointment_service, stylist_id) == []
    assert weather_calls == []

    third = asyncio.run(ask(agent, "下午两点"))
    appointment_id = re.search(r"预约编号：(\d+)", third)
    assert appointment_id
    assert finder_calls == ["find"]
    persisted = schedules_for(appointment_service, stylist_id)
    assert len(persisted) == 1
    assert persisted[0]["start_time"].strftime("%Y-%m-%d %H:%M") == "2026-07-17 14:00"
    assert persisted[0]["appointment_id"] == int(appointment_id.group(1))
    assert weather_calls == [int(appointment_id.group(1))]


def test_service_then_date_then_time_merges_slots(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)
    agent.appointment_processor.weather_tool = SimpleNamespace(
        get_weather_context=lambda appointment_time=None: None
    )

    first = asyncio.run(ask(agent, "男士短发"))
    assert "预约日期、预约时间" in first
    second = asyncio.run(ask(agent, "明天"))
    assert agent.appointment_history["requested_date"] == "2026-07-17"
    assert agent.appointment_history.get("requested_exact_time") is None

    class OmittedWeather:
        async def get_weather_context(self, appointment_time=None):
            return WeatherContextResult(status="omitted", reason="disabled")

    agent.appointment_processor.weather_tool = OmittedWeather()
    third = asyncio.run(ask(agent, "下午三点"))
    assert "预约成功" in third
    persisted = schedules_for(appointment_service, stylist_id)
    assert persisted[0]["start_time"].strftime("%H:%M") == "15:00"


def test_date_range_then_service_enters_availability_without_default_time(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)

    first = asyncio.run(ask(agent, "预约明天下午"))
    assert agent.appointment_history["requested_range_start"] == "12:00"
    assert agent.appointment_history["requested_range_end"] == "18:00"
    assert agent.appointment_history.get("start_time") is None
    assert "服务项目" in first

    second = asyncio.run(ask(agent, "男士短发"))
    assert "真实可预约选项" in second
    assert "林浩" in second
    assert agent.appointment_history["awaiting_slot_selection"] is True
    assert schedules_for(appointment_service, stylist_id) == []


def test_exact_time_then_service_creates_14_clock_booking(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)

    class OmittedWeather:
        async def get_weather_context(self, appointment_time=None):
            return WeatherContextResult(status="omitted", reason="disabled")

    agent.appointment_processor.weather_tool = OmittedWeather()
    first = asyncio.run(ask(agent, "预约明天下午两点"))
    assert agent.appointment_history["requested_exact_time"] == "14:00"
    assert "服务项目" in first
    second = asyncio.run(ask(agent, "男士短发"))
    assert "预约成功" in second
    assert schedules_for(appointment_service, stylist_id)[0]["start_time"].strftime("%H:%M") == "14:00"


def test_explicit_midnight_is_rejected_before_stylist_lookup_and_slots_are_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)
    finder_calls = []
    original_find = agent.stylist_finder.find_stylist_with_thought

    def tracked_find(*args, **kwargs):
        finder_calls.append("find")
        return original_find(*args, **kwargs)

    class ForbiddenWeather:
        async def get_weather_context(self, appointment_time=None):
            raise AssertionError("weather must not run for an invalid booking time")

    agent.stylist_finder.find_stylist_with_thought = tracked_find
    agent.appointment_processor.weather_tool = ForbiddenWeather()
    reply = asyncio.run(ask(agent, "预约明天凌晨零点男士短发"))

    assert "营业时间为10:00—21:00" in reply
    assert finder_calls == []
    assert schedules_for(appointment_service, stylist_id) == []
    assert agent.appointment_history["requested_date"] == "2026-07-17"
    assert agent.appointment_history["project"] == "男士短发"
    assert agent.appointment_history.get("requested_exact_time") is None
    assert agent.appointment_history.get("start_time") is None


def test_robot_example_and_malicious_llm_time_cannot_fill_missing_user_time(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service, stylist_id = build_partial_booking_agent(tmp_path)
    agent.chat_history = [
        SimpleNamespace(type="ai", content="例如可以选择明天下午三点"),
    ]

    asyncio.run(ask(agent, "预约明天"))
    reply = asyncio.run(ask(agent, "男士短发"))

    assert "明天希望预约" in reply
    assert agent.appointment_history.get("requested_exact_time") is None
    assert agent.appointment_history.get("start_time") is None
    assert schedules_for(appointment_service, stylist_id) == []


def test_input_parser_history_excludes_robot_messages():
    parser = InputParser.__new__(InputParser)

    class CapturingChain:
        def stream(self, values):
            assert "机器人：例如下午三点" not in values["history"]
            assert "用户：男士短发" in values["history"]
            yield SimpleNamespace(content='{"project":"男士短发"}')

    parser.chain = CapturingChain()
    history = SimpleNamespace(
        messages=[SimpleNamespace(type="ai", content="例如下午三点")],
        add_message=lambda message: history.messages.append(message),
    )
    assert "".join(parser.parse_stream("男士短发", history)) == '{"project":"男士短发"}'

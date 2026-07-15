import asyncio
from datetime import date, datetime, time, timedelta
import re
from types import SimpleNamespace

import pytest

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import AppointmentProcessor, WeatherContextResult
from agents.appointment.availability_parser import (
    CONSULTATION,
    SEARCH_AVAILABILITY,
    detect_message_intent,
    parse_availability_request,
)
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from agents.appointment_agent import AppointmentAgent
from api import chat_handler
from config.constants import StateEnum
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.availability_service import AvailabilitySearchRequest, AvailabilityService
from services.service_catalog import normalize_specialty, service_for_specialty, structured_stylist_profile
from services.user_behavior_service import UserBehaviorService


FIXED_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=time_config.BEIJING_TZ)


@pytest.mark.parametrize(
    "message",
    [
        "明天下午找擅长冷棕色的老师",
        "明天下午谁有空做染发",
        "帮我看看明天有哪些老师可以染冷棕色",
        "周五下午想染冷棕色，谁有时间",
        "找一位会做显白发色的老师",
        "明天下午找个有空的老师",
    ],
)
def test_implicit_booking_messages_route_to_appointment(message):
    assert detect_message_intent(message) == SEARCH_AVAILABILITY
    assert chat_handler.route_user_message(message) == "appointment"


@pytest.mark.parametrize(
    "message",
    [
        "冷棕色适合什么肤色？",
        "染发后怎么护理？",
        "冷棕色一般能保持多久？",
        "门店几点营业？",
        "染发有哪些注意事项？",
    ],
)
def test_consultation_questions_stay_in_consultation(message):
    assert detect_message_intent(message) == CONSULTATION
    assert chat_handler.route_user_message(message) == "consultation"


def test_backend_overrides_frontend_consultation_for_availability(monkeypatch):
    session = SimpleNamespace(
        session_id="availability-session",
        lock=asyncio.Lock(),
        task_agent=SimpleNamespace(
            appointment_agent=SimpleNamespace(appointment_history={}),
            state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY),
        ),
    )

    async def route_task_stream(message, route):
        session.task_agent.effective_route = route
        yield route

    session.task_agent.route_task_stream = route_task_stream
    monkeypatch.setattr(chat_handler, "_chat_sessions", SimpleNamespace(get_or_create=lambda _: session))

    async def collect():
        return "".join([
            token
            async for token in chat_handler.ProcessUserInput_stream(
                "明天下午找擅长冷棕色的老师",
                session_id="availability-session",
                route="consultation",
            )
        ])

    assert asyncio.run(collect()) == "appointment"
    assert session.task_agent.effective_route == "appointment"


def test_relative_date_period_and_clock_parsing():
    tomorrow = parse_availability_request("明天下午找老师", FIXED_NOW)
    day_after = parse_availability_request("后天晚上找老师", FIXED_NOW)
    friday = parse_availability_request("周五下午两点半谁有空", FIXED_NOW)
    today_morning = parse_availability_request("今天上午找老师", FIXED_NOW)
    noon = parse_availability_request("明天中午找老师", FIXED_NOW)
    clock = parse_availability_request("明天14:30谁有空", FIXED_NOW)

    assert tomorrow.target_date == date(2026, 7, 16)
    assert (tomorrow.range_start, tomorrow.range_end) == (time(12), time(18))
    assert day_after.target_date == date(2026, 7, 17)
    assert (day_after.range_start, day_after.range_end) == (time(18), time(21))
    assert friday.target_date == date(2026, 7, 17)
    assert friday.exact_time == time(14, 30)
    assert (today_morning.target_date, today_morning.range_start, today_morning.range_end) == (
        date(2026, 7, 15), time(10), time(12)
    )
    assert (noon.range_start, noon.range_end) == (time(11, 30), time(13, 30))
    assert clock.exact_time == time(14, 30)

    named = parse_availability_request(
        "周晴明天下午谁有空做染发", FIXED_NOW, stylist_names=["周晴", "林浩"]
    )
    assert named.stylist_name == "周晴"


def test_absolute_past_date_is_preserved_for_business_rejection():
    parsed = parse_availability_request("2026年7月14日下午谁有空做染发", FIXED_NOW)
    assert parsed.target_date == date(2026, 7, 14)


def test_specialty_normalization_and_service_mapping():
    assert normalize_specialty("冷棕") == "冷棕色"
    assert service_for_specialty("冷调棕色").name == "染发"
    assert service_for_specialty("显白发色").key == "color"
    assert normalize_specialty("不存在的梦幻颜色") is None


def build_availability_stack(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'availability.db'}"
    appointment_service = AppointmentService(db_url)
    stylist_ids = {
        "available": appointment_service.add_stylist("周晴", "女", "染发调色、冷棕色、显白发色"),
        "busy": appointment_service.add_stylist("顾然", "女", "染发调色、冷棕色"),
        "unsupported": appointment_service.add_stylist("林浩", "男", "男士短发、渐变推剪"),
        "other_color": appointment_service.add_stylist("苏敏", "女", "染发调色、暖棕色"),
    }
    busy_start = datetime(2026, 7, 16, 12, tzinfo=time_config.BEIJING_TZ)
    appointment_service.stylist_repo.add_schedule(
        stylist_ids["busy"], busy_start, busy_start + timedelta(hours=6), "busy", 7001
    )
    return appointment_service, stylist_ids, db_url


def cold_brown_request():
    return AvailabilitySearchRequest(
        target_date=date(2026, 7, 16),
        range_start=time(12),
        range_end=time(18),
        service_key="color",
        specialty="冷棕色",
    )


def test_structured_profiles_and_real_schedule_filtering(tmp_path):
    appointment_service, stylist_ids, _ = build_availability_stack(tmp_path)
    service = AvailabilityService(appointment_service)

    profile = structured_stylist_profile(appointment_service.get_stylist_by_id(stylist_ids["available"]))
    options = service.search_available_stylists(cold_brown_request(), now=FIXED_NOW)

    assert "color" in profile["supported_services"]
    assert "冷棕色" in profile["specialty_tags"]
    assert options
    assert {item.stylist_name for item in options} == {"周晴"}
    assert all(item.specialty_matches == ["冷棕色"] for item in options)
    assert [item.start_time for item in options] == sorted(item.start_time for item in options)
    assert [item.to_session_dict() for item in options] == [
        item.to_session_dict()
        for item in service.search_available_stylists(cold_brown_request(), now=FIXED_NOW)
    ]


def test_unknown_specialty_is_not_treated_as_a_match(tmp_path):
    appointment_service, _, _ = build_availability_stack(tmp_path)
    request = AvailabilitySearchRequest(
        target_date=date(2026, 7, 16),
        range_start=time(12),
        range_end=time(18),
        service_key="color",
        specialty="不存在的梦幻颜色",
    )
    assert AvailabilityService(appointment_service).matching_stylists(request) == []
    assert AvailabilityService(appointment_service).search_available_stylists(request, now=FIXED_NOW) == []


def test_color_duration_requires_complete_150_minute_slot(tmp_path):
    appointment_service = AppointmentService(f"sqlite:///{tmp_path / 'duration.db'}")
    stylist_id = appointment_service.add_stylist("周晴", "女", "染发调色、冷棕色")
    start = datetime(2026, 7, 16, 12, tzinfo=time_config.BEIJING_TZ)
    appointment_service.stylist_repo.add_schedule(stylist_id, start, start + timedelta(hours=5), "busy", 8001)

    options = AvailabilityService(appointment_service).search_available_stylists(
        cold_brown_request(), now=FIXED_NOW
    )

    assert options == []


def build_agent(tmp_path):
    appointment_service, _, db_url = build_availability_stack(tmp_path)
    processor = AppointmentProcessor(
        input_parser=SimpleNamespace(),
        stylist_finder=StylistFinder(appointment_service),
        message_builder=MessageBuilder(),
        appointment_database=AppointmentDatabase(
            appointment_service=appointment_service,
            user_behavior_service=UserBehaviorService(db_url),
        ),
        llm=None,
    )

    class ForbiddenInputParser:
        def parse_stream(self, *args, **kwargs):
            raise AssertionError("availability workflow must not call the real LLM")

    agent = AppointmentAgent.__new__(AppointmentAgent)
    agent.session_id = "availability-session"
    agent.unrelated_callback = None
    agent.state = SimpleNamespace(value=StateEnum.APPOINTMENT)
    agent.input_parser = ForbiddenInputParser()
    agent.stylist_finder = processor.stylist_finder
    agent.message_builder = processor.message_builder
    agent.appointment_database = processor.appointment_database
    agent.appointment_processor = processor
    agent.chat_history = []
    agent.chats_by_session_id = {}
    agent.reset()
    return agent, appointment_service


async def run_agent(agent, message):
    return "".join([token async for token in agent.run_stream(message)])


def test_multi_turn_selection_confirmation_persists_then_calls_weather(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service = build_agent(tmp_path)
    weather_events = []

    class RecordingWeather:
        async def get_weather_context(self, appointment_time=None):
            schedules = appointment_service.get_stylist_schedules(
                appointment_service.get_stylist_by_name("周晴")["id"], date(2026, 7, 16)
            )
            assert len(schedules) == 1
            assert schedules[0]["appointment_id"] is not None
            weather_events.append(("weather", schedules[0]["appointment_id"]))
            return WeatherContextResult(status="available", context="天气提醒：预计预约时段上海晴。")

    agent.appointment_processor.weather_tool = RecordingWeather()
    search_reply = asyncio.run(run_agent(agent, "明天下午找擅长冷棕色的老师"))
    assert "真实可预约选项" in search_reply
    assert "周晴" in search_reply
    assert weather_events == []
    assert appointment_service.get_stylist_schedules(
        appointment_service.get_stylist_by_name("周晴")["id"], date(2026, 7, 16)
    ) == []

    selection_reply = asyncio.run(run_agent(agent, "第一个"))
    assert "请回复“确认”或“取消”" in selection_reply
    assert weather_events == []

    confirmation_reply = asyncio.run(run_agent(agent, "确认"))
    appointment_match = re.search(r"预约编号：(\d+)", confirmation_reply)
    assert appointment_match
    assert "天气提醒" in confirmation_reply
    assert weather_events == [("weather", int(appointment_match.group(1)))]
    assert "awaiting_slot_selection" not in agent.appointment_history
    assert "awaiting_slot_confirmation" not in agent.appointment_history

    second_confirmation = asyncio.run(run_agent(agent, "确认"))
    assert "预约成功" not in second_confirmation
    assert len(appointment_service.get_stylist_schedules(
        appointment_service.get_stylist_by_name("周晴")["id"], date(2026, 7, 16)
    )) == 1


def test_candidate_selection_forms_and_ambiguity(tmp_path):
    agent, _ = build_agent(tmp_path)
    options = [
        {"option_id": 1, "stylist_name": "周晴", "start_time": "2026-07-16T14:00:00+08:00"},
        {"option_id": 2, "stylist_name": "周晴", "start_time": "2026-07-16T16:30:00+08:00"},
        {"option_id": 3, "stylist_name": "林浩", "start_time": "2026-07-16T15:00:00+08:00"},
    ]
    matcher = agent.appointment_processor._match_availability_options
    assert matcher("第一个", options)[0]["option_id"] == 1
    assert matcher("选1", options)[0]["option_id"] == 1
    assert matcher("周晴14点", options)[0]["option_id"] == 1
    assert matcher("林浩", options)[0]["option_id"] == 3
    assert len(matcher("周晴", options)) == 2


def test_cancel_clears_candidates_without_save_or_weather(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service = build_agent(tmp_path)

    class ForbiddenWeather:
        async def get_weather_context(self, appointment_time=None):
            raise AssertionError("weather must not run before a successful save")

    agent.appointment_processor.weather_tool = ForbiddenWeather()
    asyncio.run(run_agent(agent, "明天下午找擅长冷棕色的老师"))
    asyncio.run(run_agent(agent, "第一个"))
    reply = asyncio.run(run_agent(agent, "取消"))

    assert "没有写入排班" in reply
    stylist = appointment_service.get_stylist_by_name("周晴")
    assert appointment_service.get_stylist_schedules(stylist["id"], date(2026, 7, 16)) == []
    assert "pending_availability_options" not in agent.appointment_history


def test_final_confirmation_rechecks_conflict(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, appointment_service = build_agent(tmp_path)

    class ForbiddenWeather:
        async def get_weather_context(self, appointment_time=None):
            raise AssertionError("weather must not run when final conflict check fails")

    agent.appointment_processor.weather_tool = ForbiddenWeather()
    asyncio.run(run_agent(agent, "明天下午找擅长冷棕色的老师"))
    asyncio.run(run_agent(agent, "第一个"))
    option = agent.appointment_history["selected_availability_option"]
    start = datetime.fromisoformat(option["start_time"])
    end = datetime.fromisoformat(option["end_time"])
    appointment_service.stylist_repo.add_schedule(option["stylist_id"], start, end, "busy", 9001)

    reply = asyncio.run(run_agent(agent, "确认"))
    assert "刚刚变得不可用" in reply
    assert len(appointment_service.get_stylist_schedules(option["stylist_id"], date(2026, 7, 16))) == 1


def test_missing_service_and_date_continue_in_booking_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, _ = build_agent(tmp_path)
    missing_service = asyncio.run(run_agent(agent, "明天下午找个有空的老师"))
    assert "想预约剪发、染发、烫发还是其他服务" in missing_service

    agent._reset_state_after_appointment()
    missing_date = asyncio.run(run_agent(agent, "找一位会做显白发色的老师"))
    assert "希望预约哪一天" in missing_date
    assert agent.appointment_history["project"] == "染发"
    assert agent.appointment_history["specialty"] == "显白发色"


def test_past_availability_date_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, _ = build_agent(tmp_path)
    reply = asyncio.run(run_agent(agent, "2026年7月14日下午谁有空做染发"))
    assert "日期已经过去" in reply


def test_complete_new_booking_clears_old_candidate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    agent, _ = build_agent(tmp_path)
    asyncio.run(run_agent(agent, "明天下午找擅长冷棕色的老师"))
    assert agent.appointment_history["awaiting_slot_selection"] is True

    asyncio.run(run_agent(agent, "我要预约何岚2026年7月17日下午三点做造型"))
    assert "awaiting_slot_selection" not in agent.appointment_history
    assert "pending_availability_options" not in agent.appointment_history


def test_pending_candidates_are_session_scoped():
    session_a = chat_handler.ChatSession(
        session_id="a",
        task_agent=SimpleNamespace(
            appointment_agent=SimpleNamespace(appointment_history={"awaiting_slot_selection": True}),
            state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.APPOINTMENT),
        ),
    )
    session_b = chat_handler.ChatSession(
        session_id="b",
        task_agent=SimpleNamespace(
            appointment_agent=SimpleNamespace(appointment_history={}),
            state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY),
        ),
    )
    assert chat_handler.route_user_message("第一个", session_a) == "appointment"
    assert chat_handler.route_user_message("第一个", session_b) != "appointment"

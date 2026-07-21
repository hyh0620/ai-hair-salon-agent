import asyncio
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agents.appointment.availability_parser import (
    AMBIGUOUS,
    CREATE_BOOKING,
    detect_message_intent,
)
from agents.appointment.lifecycle_parser import (
    CANCEL_APPOINTMENT,
    GET_APPOINTMENT,
    LIST_APPOINTMENTS,
    RESCHEDULE_APPOINTMENT,
    UPDATE_APPOINTMENT,
    detect_lifecycle_intent,
)
from agents.appointment.lifecycle_processor import AppointmentLifecycleProcessor
from agents.appointment_agent import AppointmentAgent
from api import chat_handler
from config.constants import StateEnum
from config.time_config import time_config
from services.appointment_service import AppointmentService


def _future_start(days=60, hour=14):
    return (time_config.now() + timedelta(days=days)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )


def _setup(tmp_path):
    db_file = tmp_path / "lifecycle-chat.db"
    service = AppointmentService(f"sqlite:///{db_file}")
    stylist = service.add_stylist(
        "全能老师",
        "女",
        "男士短发、渐变推剪、染发调色、冷棕色、挑染",
    )
    return db_file, service, stylist


def _create(service, stylist, owner, *, days=60, hour=14):
    start = _future_start(days=days, hour=hour)
    details = service.build_appointment_details({"project": "男士短发", "user_id": owner})
    result = service.save_appointment_detailed(
        str(stylist),
        start,
        start + timedelta(minutes=45),
        details,
        owner,
    )
    assert result.success
    return result


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("查看我的预约", LIST_APPOINTMENTS),
        ("我预约了什么时间？", LIST_APPOINTMENTS),
        ("查询预约123", GET_APPOINTMENT),
        ("帮我取消预约", CANCEL_APPOINTMENT),
        ("取消明天下午的预约", CANCEL_APPOINTMENT),
        ("我想改一下预约", UPDATE_APPOINTMENT),
        ("我想换一个发型师", UPDATE_APPOINTMENT),
        ("把预约换到下午三点", RESCHEDULE_APPOINTMENT),
    ],
)
def test_lifecycle_intents_route_to_appointment_without_rag(text, intent):
    assert detect_lifecycle_intent(text) == intent
    assert chat_handler.route_user_message(text) == "appointment"


@pytest.mark.parametrize(
    "text",
    [
        "我想改一下明天的预约",
        "我想改一下后天下午的预约",
        "我想改一下林浩的预约",
        "我想调整一下周五的预约",
        "帮我修改明天下午的预约",
        "把明天的预约改一下",
    ],
)
def test_qualified_update_phrases_keep_lifecycle_routing(text):
    assert detect_lifecycle_intent(text) == UPDATE_APPOINTMENT
    assert detect_message_intent(text) == UPDATE_APPOINTMENT
    assert chat_handler.route_user_message(text) == "appointment"


@pytest.mark.parametrize(
    "text",
    [
        "把明天的预约换到下午三点",
        "将周五的预约改到下午四点",
        "预约改期到后天上午十点",
    ],
)
def test_qualified_reschedule_phrases_keep_lifecycle_routing(text):
    assert detect_lifecycle_intent(text) == RESCHEDULE_APPOINTMENT
    assert detect_message_intent(text) == RESCHEDULE_APPOINTMENT
    assert chat_handler.route_user_message(text) == "appointment"


@pytest.mark.parametrize(
    ("text", "intent", "route"),
    [
        ("我想修改一下发型设计方案", AMBIGUOUS, "agent"),
        ("怎么调整染发后的护理方式", AMBIGUOUS, "agent"),
        ("明天预约男士短发", CREATE_BOOKING, "appointment"),
        ("我想预约明天下午三点", CREATE_BOOKING, "appointment"),
        ("修改头发颜色会伤头发吗", AMBIGUOUS, "agent"),
    ],
)
def test_non_lifecycle_phrases_keep_existing_routing(text, intent, route):
    assert detect_lifecycle_intent(text) is None
    assert detect_message_intent(text) == intent
    assert chat_handler.route_user_message(text) == route


def test_multi_appointment_query_selection_is_stable_and_owner_scoped(tmp_path):
    _, service, stylist = _setup(tmp_path)
    first = _create(service, stylist, "session-a", days=60, hour=14)
    second = _create(service, stylist, "session-a", days=61, hour=15)
    _create(service, stylist, "session-b", days=62, hour=16)
    processor = AppointmentLifecycleProcessor(service)
    history = {}

    listed = processor.handle(
        "查看我的预约",
        history,
        "session-a",
        intent=LIST_APPOINTMENTS,
    )
    selected = processor.handle("第二个", history, "session-a")

    assert listed.complete is False
    assert f"预约{first.appointment_id}" in listed.message
    assert f"预约{second.appointment_id}" in listed.message
    assert "session-b" not in listed.message
    assert selected.complete is True
    assert f"预约编号：{second.appointment_id}" in selected.message
    assert history == {}


def test_cancel_chat_requires_confirmation_and_releases_schedule(tmp_path):
    db_file, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a")
    processor = AppointmentLifecycleProcessor(service)
    history = {}

    prompt = processor.handle(
        "帮我取消预约",
        history,
        "session-a",
        intent=CANCEL_APPOINTMENT,
    )
    with sqlite3.connect(db_file) as connection:
        before = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    confirmed = processor.handle("确认取消预约", history, "session-a")

    assert prompt.complete is False
    assert "确认取消预约" in prompt.message
    assert before == ("confirmed", 1)
    assert confirmed.complete is True
    assert "预约已取消" in confirmed.message
    with sqlite3.connect(db_file) as connection:
        appointment = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
        schedule = connection.execute(
            "SELECT status FROM stylist_schedules WHERE appointment_id=?",
            (created.appointment_id,),
        ).fetchone()
    assert appointment == ("cancelled", 2)
    assert schedule == ("cancelled",)


def test_cancel_chat_exit_and_other_owner_do_not_write(tmp_path):
    db_file, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a")
    processor = AppointmentLifecycleProcessor(service)
    own_history = {}
    other_history = {}

    processor.handle("帮我取消预约", own_history, "session-a", intent=CANCEL_APPOINTMENT)
    exited = processor.handle("保留预约", own_history, "session-a")
    hidden = processor.handle(
        f"取消预约{created.appointment_id}",
        other_history,
        "session-b",
        intent=CANCEL_APPOINTMENT,
    )

    assert exited.complete is True
    assert "保留" in exited.message
    assert hidden.complete is True
    assert "没有找到" in hidden.message
    with sqlite3.connect(db_file) as connection:
        status = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert status == ("confirmed", 1)


def test_update_chat_collects_change_previews_and_confirms(tmp_path):
    db_file, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a")
    processor = AppointmentLifecycleProcessor(service)
    history = {}

    selected = processor.handle(
        "我想改一下预约",
        history,
        "session-a",
        intent=UPDATE_APPOINTMENT,
    )
    preview = processor.handle("换到下午三点", history, "session-a")
    with sqlite3.connect(db_file) as connection:
        before = connection.execute(
            "SELECT start_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    confirmed = processor.handle("确认修改", history, "session-a")

    assert selected.complete is False
    assert "需要修改" in selected.message
    assert preview.complete is False
    assert "原预约" in preview.message and "新预约" in preview.message
    assert before[1] == 1
    assert confirmed.complete is True
    assert "预约修改成功" in confirmed.message
    with sqlite3.connect(db_file) as connection:
        after = connection.execute(
            "SELECT start_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert "15:00:00" in after[0]
    assert after[1] == 2


def test_qualified_update_phrase_uses_active_flow_and_existing_service(
    tmp_path,
    monkeypatch,
):
    db_file, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a", days=1, hour=14)
    processor = AppointmentLifecycleProcessor(service)
    history = {}
    first_message = "我想改一下明天的预约"

    selected = processor.handle(
        first_message,
        history,
        "session-a",
        intent=detect_lifecycle_intent(first_message),
    )
    task_agent = SimpleNamespace(
        appointment_agent=SimpleNamespace(appointment_history=history),
        state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY),
    )
    session = chat_handler.ChatSession("session-a", task_agent)

    assert chat_handler.route_user_message(first_message) == "appointment"
    assert selected.complete is False
    assert history["awaiting_lifecycle_changes"] is True
    assert chat_handler.route_user_message("换到下午三点", session) == "appointment"

    preview = processor.handle("换到下午三点", history, "session-a")
    original_update = service.update_appointment
    update_calls = []

    def tracked_update(*args, **kwargs):
        update_calls.append((args, kwargs))
        return original_update(*args, **kwargs)

    monkeypatch.setattr(service, "update_appointment", tracked_update)
    confirmed = processor.handle("确认修改", history, "session-a")

    assert preview.complete is False
    assert "原预约" in preview.message and "新预约" in preview.message
    assert confirmed.complete is True
    assert "预约修改成功" in confirmed.message
    assert len(update_calls) == 1
    assert update_calls[0][0][:3] == (created.appointment_id, "session-a", 1)
    assert history == {}
    with sqlite3.connect(db_file) as connection:
        after = connection.execute(
            "SELECT start_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert "15:00:00" in after[0]
    assert after[1] == 2


def test_update_chat_reloads_database_and_rejects_stale_session_version(tmp_path):
    db_file, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a")
    processor = AppointmentLifecycleProcessor(service)
    history = {}

    processor.handle("我想改一下预约", history, "session-a", intent=UPDATE_APPOINTMENT)
    processor.handle("换到下午三点", history, "session-a")
    external = service.update_appointment(
        created.appointment_id,
        "session-a",
        1,
        target_time=_future_start(hour=16).time(),
    )
    confirmed = processor.handle("确认修改", history, "session-a")

    assert external.status == "success"
    assert confirmed.complete is True
    assert "状态已变化" in confirmed.message
    with sqlite3.connect(db_file) as connection:
        row = connection.execute(
            "SELECT start_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert "16:00:00" in row[0]
    assert row[1] == 2


def test_lifecycle_candidates_and_active_short_messages_are_session_isolated(tmp_path):
    _, service, stylist = _setup(tmp_path)
    _create(service, stylist, "session-a", days=60, hour=14)
    _create(service, stylist, "session-a", days=61, hour=15)
    processor = AppointmentLifecycleProcessor(service)
    history_a = {}
    history_b = {}
    processor.handle("我想改一下预约", history_a, "session-a", intent=UPDATE_APPOINTMENT)

    appointment_agent_a = SimpleNamespace(appointment_history=history_a)
    appointment_agent_b = SimpleNamespace(appointment_history=history_b)
    state_manager = SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY)
    session_a = chat_handler.ChatSession(
        "session-a",
        SimpleNamespace(appointment_agent=appointment_agent_a, state_manager=state_manager),
    )
    session_b = chat_handler.ChatSession(
        "session-b",
        SimpleNamespace(appointment_agent=appointment_agent_b, state_manager=state_manager),
    )

    assert chat_handler.route_user_message("第一个", session_a) == "appointment"
    assert chat_handler.route_user_message("第一个", session_b) == "agent"
    assert history_b == {}


def test_backend_overrides_frontend_consultation_route_for_lifecycle_intent(monkeypatch):
    state_manager = SimpleNamespace(
        get_current_state=lambda: StateEnum.CLASSIFY,
    )
    task_agent = SimpleNamespace(
        appointment_agent=SimpleNamespace(appointment_history={}),
        state_manager=state_manager,
    )

    async def route_task_stream(_message, route, owner_id=None):
        task_agent.effective_route = route
        task_agent.owner_id = owner_id
        yield route

    task_agent.route_task_stream = route_task_stream
    session = chat_handler.ChatSession("session-a", task_agent)
    monkeypatch.setattr(
        chat_handler,
        "_chat_sessions",
        SimpleNamespace(get_or_create=lambda _session_id: session),
    )

    async def collect():
        return "".join([
            token
            async for token in chat_handler.ProcessUserInput_stream(
                "查看我的预约",
                session_id="session-a",
                owner_id="owner-a",
                route="consultation",
            )
        ])

    assert asyncio.run(collect()) == "appointment"
    assert task_agent.effective_route == "appointment"
    assert task_agent.owner_id == "owner-a"


def test_appointment_agent_uses_lifecycle_processor_and_preserves_chat_boundary(tmp_path):
    _, service, stylist = _setup(tmp_path)
    created = _create(service, stylist, "session-a")
    agent = AppointmentAgent.__new__(AppointmentAgent)
    agent.session_id = "session-a"
    agent.appointment_history = {
        "gender": None,
        "start_time": None,
        "duration": None,
        "project": None,
        "preference": None,
        "style_preference": None,
        "budget": None,
        "stylist_name": None,
    }
    agent.lifecycle_processor = AppointmentLifecycleProcessor(service)
    agent.chat_history = ["existing message"]
    agent.state = SimpleNamespace(value=StateEnum.APPOINTMENT)
    agent.finished = False

    async def collect():
        return "".join([
            token async for token in agent.run_stream("查看我的预约")
        ])

    reply = asyncio.run(collect())

    assert f"预约编号：{created.appointment_id}" in reply
    assert agent.chat_history == ["existing message"]
    assert agent.state.value == StateEnum.CLASSIFY

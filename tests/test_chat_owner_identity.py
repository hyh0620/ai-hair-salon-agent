import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.appointment.lifecycle_parser import (
    CANCEL_APPOINTMENT,
    GET_APPOINTMENT,
    LIST_APPOINTMENTS,
    detect_lifecycle_intent,
)
from agents.appointment.availability_parser import detect_message_intent
from api import chat_handler
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService
from web.routes import router as web_router


FIXED_NOW = datetime(2026, 7, 20, 9, 0, tzinfo=time_config.BEIJING_TZ)


def _configure_chat(monkeypatch, tmp_path):
    db_file = tmp_path / "chat-owner.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("MODEL_PROVIDER", "qwen")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    assert StylistService(db_url).initialize_default_stylists()
    registry = chat_handler.ChatSessionRegistry(ttl_seconds=3600, max_sessions=20)
    monkeypatch.setattr(chat_handler, "_chat_sessions", registry)
    return db_file, db_url, AppointmentService(db_url), registry


def _create_appointment(service, owner_id, *, day_offset=30, hour=14):
    stylist = service.get_stylist_by_name("林浩")
    start = (FIXED_NOW + timedelta(days=day_offset)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    details = service.build_appointment_details({
        "project": "男士短发",
        "user_id": owner_id,
    })
    result = service.save_appointment_detailed(
        str(stylist["id"]),
        start,
        start + timedelta(minutes=45),
        details,
        "setup-session",
        owner_id=owner_id,
    )
    assert result.success
    return result


def _chat(message, *, session_id, owner_id, route="appointment"):
    async def collect():
        return "".join([
            token
            async for token in chat_handler.ProcessUserInput_stream(
                message,
                session_id=session_id,
                owner_id=owner_id,
                route=route,
            )
        ])

    return asyncio.run(collect())


def test_chat_booking_persists_owner_separately_from_session(monkeypatch, tmp_path):
    db_file, _, _, _ = _configure_chat(monkeypatch, tmp_path)
    session_id = "chat-session-a"
    owner_id = "anonymous-owner-a"

    options = _chat(
        "预约2026年8月20日下午两点做男士短发",
        session_id=session_id,
        owner_id=owner_id,
    )
    selected = _chat("第一个", session_id=session_id, owner_id=owner_id)
    confirmed = _chat("确认", session_id=session_id, owner_id=owner_id)

    assert "可预约" in options
    assert "确认" in selected
    assert "预约成功" in confirmed
    with sqlite3.connect(db_file) as connection:
        persisted = connection.execute(
            "SELECT user_id, session_id FROM appointments"
        ).fetchone()
    assert persisted == (owner_id, session_id)


def test_reset_keeps_owner_access_for_query_update_and_cancel(monkeypatch, tmp_path):
    db_file, _, service, registry = _configure_chat(monkeypatch, tmp_path)
    owner_id = "stable-owner"
    created = _create_appointment(service, owner_id)
    old_session_id = "chat-before-reset"
    registry.get_or_create(old_session_id)

    with sqlite3.connect(db_file) as connection:
        before_reset = connection.execute(
            "SELECT id, status, version FROM appointments ORDER BY id"
        ).fetchall()
    new_session_id = registry.reset(old_session_id)
    assert new_session_id != old_session_id
    assert registry.get_existing(old_session_id) is None
    with sqlite3.connect(db_file) as connection:
        after_reset = connection.execute(
            "SELECT id, status, version FROM appointments ORDER BY id"
        ).fetchall()
    assert after_reset == before_reset == [(created.appointment_id, "confirmed", 1)]

    queried = _chat("查看预约", session_id=new_session_id, owner_id=owner_id)
    assert f"预约编号：{created.appointment_id}" in queried

    update_started = _chat("修改预约", session_id=new_session_id, owner_id=owner_id)
    update_preview = _chat("换到下午三点", session_id=new_session_id, owner_id=owner_id)
    update_done = _chat("确认修改", session_id=new_session_id, owner_id=owner_id)
    assert "需要修改" in update_started
    assert "新预约" in update_preview
    assert "预约修改成功" in update_done

    final_session_id = registry.reset(new_session_id)
    cancel_started = _chat("取消预约", session_id=final_session_id, owner_id=owner_id)
    cancel_done = _chat("确认取消预约", session_id=final_session_id, owner_id=owner_id)
    assert "确认是否取消" in cancel_started
    assert "预约已取消" in cancel_done
    with sqlite3.connect(db_file) as connection:
        appointment = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
        schedule = connection.execute(
            "SELECT status FROM stylist_schedules WHERE appointment_id=?",
            (created.appointment_id,),
        ).fetchone()
    assert appointment == ("cancelled", 3)
    assert schedule == ("cancelled",)


def test_expired_session_does_not_remove_owner_appointment_access(monkeypatch, tmp_path):
    _, _, service, registry = _configure_chat(monkeypatch, tmp_path)
    owner_id = "ttl-owner"
    created = _create_appointment(service, owner_id)
    registry.ttl_seconds = 1
    old_session = registry.get_or_create("expired-session")
    old_session.last_access = time.monotonic() - 10

    registry.get_or_create("replacement-session")

    assert registry.get_existing("expired-session") is None
    reply = _chat(
        "查询预约",
        session_id="replacement-session",
        owner_id=owner_id,
    )
    assert f"预约编号：{created.appointment_id}" in reply


@pytest.mark.parametrize("operation", ["查询", "修改", "取消"])
def test_other_owner_cannot_manage_appointment(monkeypatch, tmp_path, operation):
    _, _, service, _ = _configure_chat(monkeypatch, tmp_path)
    created = _create_appointment(service, "owner-a")

    reply = _chat(
        f"{operation}预约编号 {created.appointment_id}",
        session_id=f"owner-b-{operation}",
        owner_id="owner-b",
    )

    assert "没有找到" in reply
    own = service.get_user_appointment(created.appointment_id, "owner-a")
    assert own.success
    assert own.appointment["status"] == "confirmed"
    assert own.appointment["version"] == 1


def test_saved_cancel_interrupts_candidate_selection(monkeypatch, tmp_path):
    db_file, _, service, registry = _configure_chat(monkeypatch, tmp_path)
    owner_id = "candidate-owner"
    created = _create_appointment(service, owner_id)
    session_id = "candidate-session"
    session = registry.get_or_create(session_id)
    session.task_agent.appointment_agent.appointment_history.update({
        "availability_search_active": True,
        "awaiting_slot_selection": True,
        "pending_availability_options": [{
            "option_id": 1,
            "stylist_id": 1,
            "stylist_name": "候选老师",
            "start_time": "2026-08-25T14:00:00",
        }],
    })

    prompt = _chat("取消预约", session_id=session_id, owner_id=owner_id)

    history = session.task_agent.appointment_agent.appointment_history
    assert "没有匹配到该选项" not in prompt
    assert "确认是否取消" in prompt
    assert history["pending_lifecycle_action"] == "cancel"
    assert "awaiting_slot_selection" not in history
    with sqlite3.connect(db_file) as connection:
        status = connection.execute(
            "SELECT status FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert status == ("confirmed",)


@pytest.mark.parametrize("text", ["取消本次操作", "退出当前操作", "不用了", "先不预约了", "退出预约流程", "取消"])
def test_abort_creation_clears_state_without_touching_saved_appointment(
    monkeypatch,
    tmp_path,
    text,
):
    db_file, _, service, registry = _configure_chat(monkeypatch, tmp_path)
    owner_id = "abort-owner"
    created = _create_appointment(service, owner_id)
    session_id = f"abort-{len(text)}"
    session = registry.get_or_create(session_id)
    session.task_agent.appointment_agent.appointment_history.update({
        "availability_search_active": True,
        "awaiting_slot_selection": True,
        "pending_availability_options": [{"option_id": 1}],
        "requested_date": "2026-08-20",
        "project": "男士短发",
    })

    reply = _chat(text, session_id=session_id, owner_id=owner_id)

    assert "已退出本次预约操作" in reply
    assert "已有预约不会受到影响" in reply
    current_history = session.task_agent.appointment_agent.appointment_history
    assert "awaiting_slot_selection" not in current_history
    assert "pending_availability_options" not in current_history
    with sqlite3.connect(db_file) as connection:
        status = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert status == ("confirmed", 1)


def test_bare_cancel_without_active_flow_requires_clarification(monkeypatch, tmp_path):
    _configure_chat(monkeypatch, tmp_path)

    reply = _chat("取消", session_id="idle-session", owner_id="idle-owner")

    assert "取消已保存的预约" in reply
    assert "取消预约" in reply
    assert "取消本次操作" in reply


def test_bare_cancel_abandons_saved_cancel_confirmation(monkeypatch, tmp_path):
    db_file, _, service, _ = _configure_chat(monkeypatch, tmp_path)
    owner_id = "keep-owner"
    created = _create_appointment(service, owner_id)
    session_id = "keep-session"

    prompt = _chat("取消预约", session_id=session_id, owner_id=owner_id)
    kept = _chat("取消", session_id=session_id, owner_id=owner_id)

    assert "确认是否取消" in prompt
    assert "已保留原预约" in kept
    with sqlite3.connect(db_file) as connection:
        row = connection.execute(
            "SELECT status, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert row == ("confirmed", 1)


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("查看预约", LIST_APPOINTMENTS),
        ("查看预约？", LIST_APPOINTMENTS),
        ("查询预约", LIST_APPOINTMENTS),
        ("查询预约。", LIST_APPOINTMENTS),
        ("看看预约", LIST_APPOINTMENTS),
        ("我的预约", LIST_APPOINTMENTS),
        ("预约记录", LIST_APPOINTMENTS),
        ("查看预约编号 12", GET_APPOINTMENT),
        ("预约编号 12 的详情", GET_APPOINTMENT),
        ("取消我的预约", CANCEL_APPOINTMENT),
    ],
)
def test_deterministic_lifecycle_aliases(text, intent):
    assert detect_lifecycle_intent(text) == intent
    assert detect_message_intent(text) == intent
    assert chat_handler.route_user_message(text) == "appointment"


def test_booking_date_is_not_mistaken_for_appointment_id():
    text = "预约2026年7月27日下午两点做男士短发"

    assert detect_lifecycle_intent(text) is None


def test_legacy_owner_fallback_is_explicit_and_logged(caplog):
    with caplog.at_level(logging.WARNING):
        owner_id = chat_handler.resolve_owner_id(None, "legacy-session")

    assert owner_id == "legacy-session"
    assert "chat_owner_fallback_deprecated" in caplog.text
    assert "legacy-session" in caplog.text


def test_chat_owner_contract_rejects_invalid_values():
    app = FastAPI()
    app.include_router(web_router)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/chat/route",
            json={
                "message": "查看预约",
                "session_id": "session-a",
                "owner_id": "owner-a",
            },
        )
        rejected = client.post(
            "/api/chat/route",
            json={
                "message": "查看预约",
                "session_id": "session-a",
                "owner_id": "owner/invalid",
            },
        )

    assert accepted.status_code == 200
    assert accepted.json() == {"route": "appointment"}
    assert rejected.status_code == 422


def test_direct_chat_call_rejects_invalid_owner_without_creating_fallback(monkeypatch):
    session = chat_handler.ChatSession(
        "valid-session",
        type("TaskAgent", (), {})(),
    )

    class Registry:
        def get_or_create(self, session_id):
            assert session_id == "valid-session"
            return session

    monkeypatch.setattr(chat_handler, "_chat_sessions", Registry())

    async def collect():
        return "".join([
            token
            async for token in chat_handler.ProcessUserInput_stream(
                "查看预约",
                session_id="valid-session",
                owner_id="invalid/owner",
                route="appointment",
            )
        ])

    reply = asyncio.run(collect())

    assert "客户端预约标识无效" in reply


def test_browser_identity_migration_and_reset_contract():
    html = Path("web/templates/index.html").read_text(encoding="utf-8")
    identity_block = html[html.index("function getOrCreateBrowserIdentity"):html.index("function addMessage")]
    clear_block = html[html.index("clearBtn.onclick"):html.index("// 添加回车键")]

    assert "salon_chat_session_id" in html
    assert "salon_anonymous_owner_id" in html
    assert "if (existingOwnerId)" in identity_block
    assert "ownerId = existingSessionId" in identity_block
    assert "while (ownerId === sessionId)" in identity_block
    assert "setItem(sessionStorageKey, sessionId)" in clear_block
    assert "setItem(ownerStorageKey" not in clear_block


def test_consultation_route_keeps_owner_out_of_session_lookup(monkeypatch):
    session = chat_handler.ChatSession(
        "consult-session",
        type("TaskAgent", (), {})(),
    )
    session.task_agent.appointment_agent = type("Appointment", (), {"appointment_history": {}})()
    session.task_agent.state_manager = type(
        "State",
        (),
        {"get_current_state": lambda self: type("Value", (), {"value": "classify"})()},
    )()
    calls = {}

    async def route_task_stream(message, route, owner_id=None):
        calls.update(message=message, route=route, owner_id=owner_id)
        yield "consultation-ok"

    session.task_agent.route_task_stream = route_task_stream

    class Registry:
        def get_or_create(self, session_id):
            calls["session_lookup"] = session_id
            return session

    monkeypatch.setattr(chat_handler, "_chat_sessions", Registry())

    reply = _chat(
        "染发后如何护理？",
        session_id="consult-session",
        owner_id="consult-owner",
        route="consultation",
    )

    assert reply == "consultation-ok"
    assert calls == {
        "session_lookup": "consult-session",
        "message": "染发后如何护理？",
        "route": "consultation",
        "owner_id": "consult-owner",
    }

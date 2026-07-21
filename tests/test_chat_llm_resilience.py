import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agents.appointment.availability_parser import CREATE_BOOKING, detect_message_intent
from agents.appointment.lifecycle_parser import detect_lifecycle_intent, extract_appointment_id
from api import chat_handler
from config.constants import StateEnum
from config.model_provider import (
    ChatModelError,
    UnavailableChatModel,
    classify_chat_model_error,
    create_chat_model,
)
from config import model_provider
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService


FIXED_NOW = datetime(2026, 7, 20, 9, 0, tzinfo=time_config.BEIJING_TZ)


def _configure_without_llm(monkeypatch, tmp_path):
    db_url = f"sqlite:///{tmp_path / 'chat-no-llm.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("MODEL_PROVIDER", "qwen")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    for key in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_VERSION",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(time_config, "now", lambda: FIXED_NOW)
    return db_url


async def _collect_stream(
    message,
    *,
    session_id="session-a",
    owner_id=None,
    route="appointment",
):
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

    return await asyncio.wait_for(collect(), timeout=1)


def test_unconfigured_model_uses_non_networking_placeholder(monkeypatch, tmp_path):
    _configure_without_llm(monkeypatch, tmp_path)

    model = create_chat_model()

    assert isinstance(model, UnavailableChatModel)
    assert model.reason == "not_configured"
    with pytest.raises(ChatModelError, match="not_configured"):
        model.invoke("需要模型解析的消息")


def test_chat_http_clients_use_configured_local_address(monkeypatch):
    sync_transport = object()
    async_transport = object()
    sync_client = object()
    async_client = object()
    # Exercise normal runtime construction with every HTTP object replaced by a fake.
    monkeypatch.setenv("EXTERNAL_CALL_POLICY", "allow")
    monkeypatch.setenv("LLM_HTTP_LOCAL_ADDRESS", "0.0.0.0")
    monkeypatch.setattr(
        httpx,
        "HTTPTransport",
        lambda *, local_address: sync_transport if local_address == "0.0.0.0" else None,
    )
    monkeypatch.setattr(
        httpx,
        "AsyncHTTPTransport",
        lambda *, local_address: async_transport if local_address == "0.0.0.0" else None,
    )
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *, transport: sync_client if transport is sync_transport else None,
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *, transport: async_client if transport is async_transport else None,
    )

    clients = model_provider._chat_http_clients()

    assert clients == {
        "http_client": sync_client,
        "http_async_client": async_client,
    }


def test_unconfigured_llm_stream_returns_clear_message_and_ends(monkeypatch, tmp_path):
    _configure_without_llm(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_handler, "_chat_sessions", chat_handler.ChatSessionRegistry())

    reply = asyncio.run(_collect_stream("预约明天", session_id="no-llm-partial"))

    assert "[REPLY][系统]" in reply
    assert "当前未配置语言模型" in reply
    assert "LLM_API_KEY" in reply
    assert "Missing credentials" not in reply


def test_complete_unassigned_booking_is_deterministic_without_llm(monkeypatch, tmp_path):
    db_url = _configure_without_llm(monkeypatch, tmp_path)
    assert StylistService(db_url).initialize_default_stylists()
    monkeypatch.setattr(chat_handler, "_chat_sessions", chat_handler.ChatSessionRegistry())
    message = "预约2026年7月27日下午两点做男士短发"

    reply = asyncio.run(_collect_stream(message, session_id="deterministic-create"))

    assert extract_appointment_id(message) is None
    assert detect_lifecycle_intent(message) is None
    assert detect_message_intent(message) == CREATE_BOOKING
    assert chat_handler.route_user_message(message) == "appointment"
    assert "[REPLY][预约机器人]" in reply
    assert "真实可预约" in reply
    assert "当前未配置语言模型" not in reply
    with sqlite3.connect(tmp_path / "chat-no-llm.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (TimeoutError("provider took too long"), "timeout"),
        (type("AuthenticationFailure", (Exception,), {"status_code": 401})("bad"), "invalid_credentials"),
        (type("ProviderFailure", (Exception,), {"status_code": 503})("down"), "provider_unavailable"),
        (RuntimeError("unexpected internal detail"), "unexpected_error"),
    ],
)
def test_model_failures_have_stable_statuses(exception, reason):
    assert classify_chat_model_error(exception) == reason


@pytest.mark.parametrize(
    ("stream_error", "expected_message"),
    [
        (RuntimeError("private provider detail"), "处理消息时发生异常"),
        (TimeoutError("private timeout detail"), "语言模型响应超时"),
        (
            type("AuthenticationFailure", (Exception,), {"status_code": 401})("private auth detail"),
            "语言模型凭据无效",
        ),
        (
            type("ProviderFailure", (Exception,), {"status_code": 503})("private outage detail"),
            "语言模型服务暂时不可用",
        ),
    ],
)
def test_stream_generator_errors_return_safe_terminal_reply(
    monkeypatch,
    stream_error,
    expected_message,
):
    async def route_task_stream(_message, _route, owner_id=None):
        raise stream_error
        yield  # pragma: no cover - makes this an async generator

    task_agent = SimpleNamespace(
        appointment_agent=SimpleNamespace(appointment_history={}),
        state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY),
        route_task_stream=route_task_stream,
    )
    session = chat_handler.ChatSession("error-session", task_agent)
    monkeypatch.setattr(
        chat_handler,
        "_chat_sessions",
        SimpleNamespace(get_or_create=lambda _session_id: session),
    )

    reply = asyncio.run(_collect_stream("预约明天", session_id="error-session"))

    assert reply.startswith("[REPLY][系统]")
    assert expected_message in reply
    assert "private" not in reply


def test_empty_agent_stream_gets_nonempty_terminal_reply(monkeypatch):
    async def route_task_stream(_message, _route, owner_id=None):
        if False:
            yield ""

    task_agent = SimpleNamespace(
        appointment_agent=SimpleNamespace(appointment_history={}),
        state_manager=SimpleNamespace(get_current_state=lambda: StateEnum.CLASSIFY),
        route_task_stream=route_task_stream,
    )
    session = chat_handler.ChatSession("empty-session", task_agent)
    monkeypatch.setattr(
        chat_handler,
        "_chat_sessions",
        SimpleNamespace(get_or_create=lambda _session_id: session),
    )

    reply = asyncio.run(_collect_stream("预约明天", session_id="empty-session"))

    assert reply == "[REPLY][系统]服务未返回有效内容，请稍后重试。"


def test_update_lifecycle_remains_deterministic_without_llm(monkeypatch, tmp_path):
    db_url = _configure_without_llm(monkeypatch, tmp_path)
    service = AppointmentService(db_url)
    stylist_id = service.add_stylist("全能老师", "女", "男士短发、渐变推剪")
    start = (FIXED_NOW + timedelta(days=30)).replace(
        hour=14,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    details = service.build_appointment_details(
        {"project": "男士短发", "user_id": "modify-no-llm"}
    )
    created = service.save_appointment_detailed(
        str(stylist_id),
        start,
        start + timedelta(minutes=45),
        details,
        "modify-no-llm",
    )
    assert created.success
    monkeypatch.setattr(chat_handler, "_chat_sessions", chat_handler.ChatSessionRegistry())

    async def run_flow():
        selected = await _collect_stream("修改我的预约", session_id="modify-no-llm")
        preview = await _collect_stream("换到下午三点", session_id="modify-no-llm")
        confirmed = await _collect_stream("确认修改", session_id="modify-no-llm")
        return selected, preview, confirmed

    selected, preview, confirmed = asyncio.run(run_flow())

    assert "需要修改" in selected
    assert "原预约" in preview and "新预约" in preview
    assert "预约修改成功" in confirmed
    assert "当前未配置语言模型" not in selected + preview + confirmed
    with sqlite3.connect(tmp_path / "chat-no-llm.db") as connection:
        appointment = connection.execute(
            "SELECT id, start_time, end_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
        schedules = connection.execute(
            "SELECT start_time, end_time, status, appointment_id "
            "FROM stylist_schedules WHERE appointment_id=?",
            (created.appointment_id,),
        ).fetchall()
        count = connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert appointment[0] == created.appointment_id
    assert "15:00:00" in appointment[1]
    assert "15:45:00" in appointment[2]
    assert appointment[3] == 2
    assert schedules == [(appointment[1], appointment[2], "busy", created.appointment_id)]
    assert count == 1


def test_fuzzy_update_period_requests_exact_time_without_writing(monkeypatch, tmp_path):
    db_url = _configure_without_llm(monkeypatch, tmp_path)
    service = AppointmentService(db_url)
    stylist_id = service.add_stylist("全能老师", "女", "男士短发、渐变推剪")
    start = (FIXED_NOW + timedelta(days=30)).replace(
        hour=14,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    details = service.build_appointment_details(
        {"project": "男士短发", "user_id": "fuzzy-update"}
    )
    created = service.save_appointment_detailed(
        str(stylist_id),
        start,
        start + timedelta(minutes=45),
        details,
        "fuzzy-update",
    )
    assert created.success
    monkeypatch.setattr(chat_handler, "_chat_sessions", chat_handler.ChatSessionRegistry())

    async def run_flow():
        await _collect_stream("修改我的预约", session_id="fuzzy-update")
        return await _collect_stream("换到下午", session_id="fuzzy-update")

    reply = asyncio.run(run_flow())

    assert "再提供一个具体时间" in reply
    with sqlite3.connect(tmp_path / "chat-no-llm.db") as connection:
        row = connection.execute(
            "SELECT start_time, version FROM appointments WHERE id=?",
            (created.appointment_id,),
        ).fetchone()
    assert "14:00:00" in row[0]
    assert row[1] == 1


def test_homepage_handles_http_empty_and_exception_paths():
    html = Path("web/templates/index.html").read_text(encoding="utf-8")

    assert "if (!res.ok) throw new Error(`chat_stream_http_${res.status}`)" in html
    assert "if (!res.body) throw new Error('chat_stream_body_unavailable')" in html
    assert "if (!botMsg.trim()) throw new Error('chat_stream_empty')" in html
    assert "function renderBotError(message)" in html
    assert "function hasVisibleBotMessage()" in html
    finally_block = html.split("} finally {", 1)[1]
    assert "hideTypingIndicator();" in finally_block
    assert "if (!hasVisibleBotMessage())" in finally_block

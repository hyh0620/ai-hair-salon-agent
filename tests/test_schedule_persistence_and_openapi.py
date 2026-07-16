import sqlite3
from datetime import date, datetime

from fastapi.testclient import TestClient

from app import create_app
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.mcp_knowledge_gateway import MCPKnowledgeGateway, reset_mcp_knowledge_gateway


def _disabled_gateway():
    return MCPKnowledgeGateway(False, "", "src.mcp_server.server", "", "salon_knowledge", 4)


def test_future_schedule_persists_and_returns_real_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(
        time_config,
        "now",
        lambda: datetime(2026, 7, 14, 9, 0, tzinfo=time_config.BEIJING_TZ),
    )
    db_file = tmp_path / "persistent_schedule.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    reset_mcp_knowledge_gateway(_disabled_gateway())
    payload = {
        "user_id": "persistence-test",
        "project": "男士短发",
        "start_time": "2026-07-15 14:00",
        "duration": "45分钟",
        "stylist": "林浩",
    }

    try:
        with TestClient(create_app()) as client:
            created = client.post("/api/appointment/create", json=payload)
            assert created.status_code == 200
            appointment = created.json()["data"]
            assert appointment["appointment_id"] is not None

        # New service and application instances simulate a process restart.
        restarted_service = AppointmentService(f"sqlite:///{db_file}")
        persisted = restarted_service.get_stylist_schedules(appointment["stylist_id"], date(2026, 7, 15))
        assert len(persisted) == 1
        assert persisted[0]["appointment_id"] == appointment["appointment_id"]
        schedule_id = persisted[0]["id"]

        with TestClient(create_app()) as restarted_client:
            all_schedules = restarted_client.get("/api/stylists/schedules?date=2026-07-15")
            single_schedule = restarted_client.get(
                f"/api/stylists/{appointment['stylist_id']}/schedule?date=2026-07-15"
            )
            page = restarted_client.get("/stylist-schedule?date=2026-07-15")
            conflict = restarted_client.post(
                "/api/appointment/create",
                json=payload | {"user_id": "persistence-conflict"},
            )
            invalid_date = restarted_client.get("/api/stylists/schedules?date=2026-15-99")
            today = restarted_client.get("/api/stylists/schedules/today")

        assert all_schedules.status_code == 200
        body = all_schedules.json()
        linhao = next(item for item in body["stylists"] if item["stylist_name"] == "林浩")
        assert linhao["busy_periods"] == [{
            "schedule_id": schedule_id,
            "appointment_id": appointment["appointment_id"],
            "start": "14:00",
            "end": "14:45",
            "status": "busy",
        }]
        assert single_schedule.status_code == 200
        assert single_schedule.json()[0]["appointment_id"] == appointment["appointment_id"]
        assert conflict.status_code == 409
        assert invalid_date.status_code == 422
        assert today.status_code == 200
        assert page.status_code == 200
        assert page.template.name == "stylist_schedule.html"
        assert "request" in page.context
        assert page.context["selected_date"] == "2026-07-15"
        assert "所选日期：2026-07-15" in page.text
        assert f"预约编号：{appointment['appointment_id']}" in page.text
        assert f"排班编号：{schedule_id}" in page.text

        with sqlite3.connect(db_file) as connection:
            count = connection.execute(
                "select count(*) from stylist_schedules where appointment_id = ?",
                (appointment["appointment_id"],),
            ).fetchone()[0]
        assert count == 1
    finally:
        reset_mcp_knowledge_gateway(None)


def test_health_status_page_and_openapi_boundaries(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'status.db'}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    reset_mcp_knowledge_gateway(_disabled_gateway())
    try:
        with TestClient(create_app()) as client:
            home = client.get("/")
            health = client.get("/health")
            status_page = client.get("/status")
            schema = client.get("/openapi.json").json()

        assert home.status_code == 200
        assert home.template.name == "index.html"
        assert "request" in home.context
        assert health.status_code == 200
        assert health.headers["content-type"].startswith("application/json")
        assert health.json()["app"] == "healthy"
        assert status_page.status_code == 200
        assert status_page.template.name == "system_status.html"
        assert {"request", "status", "updated_at", "version"} <= status_page.context.keys()
        assert "理发店智能预约 AI Agent" in status_page.text

        paths = schema["paths"]
        for visible in (
            "/health",
            "/api/appointment/create",
            "/api/consultation/query",
            "/api/task/classify",
            "/api/knowledge/",
            "/api/stylists/",
            "/api/stylists/schedules",
            "/api/stylists/schedules/today",
            "/api/stylists/{stylist_id}",
            "/api/stylists/{stylist_id}/schedule",
            "/api/user-behavior/analysis",
            "/api/user-behavior/dashboard_data",
            "/api/user-behavior/send-reminder",
        ):
            assert visible in paths

        for hidden in (
            "/",
            "/status",
            "/chat",
            "/chat/stream",
            "/api/chat/reset",
            "/api/chat/route",
            "/api/consultation/ask",
            "/api/knowledge/reconnect",
            "/api/user_behavior/dashboard_data",
            "/knowledge",
            "/stylists",
            "/stylist-schedule",
        ):
            assert hidden not in paths
    finally:
        reset_mcp_knowledge_gateway(None)

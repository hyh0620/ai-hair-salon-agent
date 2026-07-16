import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.service_catalog import normalize_service
from services.stylist_service import StylistService


DATASET = Path(__file__).resolve().parents[1] / "eval" / "golden_dataset.jsonl"


@pytest.fixture(autouse=True)
def fixed_booking_clock(monkeypatch):
    monkeypatch.setattr(
        time_config,
        "now",
        lambda: datetime(2026, 7, 11, 9, 0, tzinfo=time_config.BEIJING_TZ),
    )


def test_booking_golden_cases_reference_known_catalog_services():
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    booking_cases = [case for case in cases if case.get("evaluation_mode") == "booking_api"]

    for case in booking_cases:
        payload = case.get("request_json", {})
        service_value = payload.get("project") or payload.get("service")
        if service_value:
            assert normalize_service(service_value), case["id"]


def test_outside_business_hours_and_conflict_are_deterministic(tmp_path):
    db_path = f"sqlite:///{tmp_path / 'salon.db'}"
    StylistService(db_path).initialize_default_stylists()
    service = AppointmentService(db_path)
    stylist = service.get_stylist_by_name("林浩")

    details = service.build_appointment_details({
        "project": "男士短发",
        "start_time": "2026-07-12 14:00",
        "duration": "45分钟",
    })
    start = datetime(2026, 7, 12, 14, 0)
    end = start + timedelta(minutes=45)

    assert service.is_within_business_hours(start, end)
    assert service.save_appointment(str(stylist["id"]), start, end, details, "first")
    assert not service.save_appointment(str(stylist["id"]), start, end, details, "second")

    late_start = datetime(2026, 7, 12, 23, 0)
    late_end = late_start + timedelta(minutes=45)
    assert not service.is_within_business_hours(late_start, late_end)


def test_api_blocks_conflict_when_stylist_alias_is_specified(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_conflict.db'}")

    payload = {
        "user_id": "api_conflict_user",
        "project": "男士短发",
        "start_time": "2026-07-12 14:00",
        "duration": "45分钟",
        "stylist": "林浩",
        "style_preference": "渐变推剪",
    }

    with TestClient(create_app()) as client:
        first = client.post("/api/appointment/create", json=payload)
        second = client.post("/api/appointment/create", json=payload | {"user_id": "api_conflict_user_2"})

    assert first.status_code == 200
    assert first.json()["data"]["stylist_name"] == "林浩"
    assert second.status_code == 409

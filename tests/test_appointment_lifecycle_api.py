import sqlite3
from datetime import time, timedelta

from fastapi.testclient import TestClient

from app import create_app
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService


def _future_start(days=45, hour=14):
    return (time_config.now() + timedelta(days=days)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )


def _setup(monkeypatch, tmp_path):
    db_file = tmp_path / "lifecycle-api.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    StylistService(db_url).initialize_default_stylists()
    service = AppointmentService(db_url)
    stylist = service.get_stylist_by_name("林浩")
    return db_file, service, stylist


def _create(service, stylist_id, owner, start):
    details = service.build_appointment_details({"project": "男士短发", "user_id": owner})
    result = service.save_appointment_detailed(
        str(stylist_id),
        start,
        start + timedelta(minutes=45),
        details,
        owner,
    )
    assert result.success
    return result


def test_lifecycle_api_lists_and_reads_only_owned_appointments(monkeypatch, tmp_path):
    _, service, stylist = _setup(monkeypatch, tmp_path)
    own = _create(service, stylist["id"], "api-owner", _future_start())
    other = _create(service, stylist["id"], "other-owner", _future_start(hour=16))

    with TestClient(create_app()) as client:
        listed = client.get("/api/appointment", params={"user_id": "api-owner"})
        detail = client.get(
            f"/api/appointment/{own.appointment_id}",
            params={"user_id": "api-owner"},
        )
        hidden = client.get(
            f"/api/appointment/{other.appointment_id}",
            params={"user_id": "api-owner"},
        )

    assert listed.status_code == 200
    assert [item["appointment_id"] for item in listed.json()["data"]["appointments"]] == [
        own.appointment_id
    ]
    assert detail.status_code == 200
    assert detail.json()["data"]["appointment"]["version"] == 1
    assert hidden.status_code == 404
    assert hidden.json()["data"]["status"] == "not_found"
    assert "ownership" not in hidden.text


def test_cancel_api_has_typed_idempotent_and_stale_contract(monkeypatch, tmp_path):
    db_file, service, stylist = _setup(monkeypatch, tmp_path)
    first = _create(service, stylist["id"], "api-owner", _future_start())
    second = _create(service, stylist["id"], "api-owner", _future_start(hour=16))

    with TestClient(create_app()) as client:
        cancelled = client.post(
            f"/api/appointment/{first.appointment_id}/cancel",
            json={"user_id": "api-owner", "expected_version": 1},
        )
        repeated = client.post(
            f"/api/appointment/{first.appointment_id}/cancel",
            json={"user_id": "api-owner", "expected_version": 1},
        )
        stale = client.post(
            f"/api/appointment/{second.appointment_id}/cancel",
            json={"user_id": "api-owner", "expected_version": 2},
        )

    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "success"
    assert cancelled.json()["data"]["current_version"] == 2
    assert repeated.status_code == 200
    assert repeated.json()["data"]["status"] == "already_cancelled"
    assert stale.status_code == 409
    assert stale.json()["data"]["status"] == "stale_state"
    with sqlite3.connect(db_file) as connection:
        rows = connection.execute(
            "SELECT a.status, s.status FROM appointments a "
            "JOIN stylist_schedules s ON s.appointment_id=a.id WHERE a.id=?",
            (first.appointment_id,),
        ).fetchone()
    assert rows == ("cancelled", "cancelled")


def test_patch_api_recalculates_catalog_and_returns_stable_errors(monkeypatch, tmp_path):
    _, service, stylist = _setup(monkeypatch, tmp_path)
    created = _create(service, stylist["id"], "api-owner", _future_start())

    with TestClient(create_app()) as client:
        changed = client.patch(
            f"/api/appointment/{created.appointment_id}",
            json={
                "user_id": "api-owner",
                "expected_version": 1,
                "target_date": _future_start(days=50).date().isoformat(),
                "start_time": "15:00",
            },
        )
        no_change = client.patch(
            f"/api/appointment/{created.appointment_id}",
            json={
                "user_id": "api-owner",
                "expected_version": 2,
                "target_date": _future_start(days=50).date().isoformat(),
                "start_time": "15:00",
            },
        )
        stale = client.patch(
            f"/api/appointment/{created.appointment_id}",
            json={
                "user_id": "api-owner",
                "expected_version": 1,
                "start_time": "16:00",
            },
        )

    assert changed.status_code == 200
    assert changed.json()["data"]["status"] == "success"
    assert changed.json()["data"]["appointment"]["duration_minutes"] == 45
    assert changed.json()["data"]["appointment"]["price"] == 88
    assert no_change.status_code == 200
    assert no_change.json()["data"]["status"] == "no_change"
    assert stale.status_code == 409
    assert stale.json()["data"]["status"] == "stale_state"


def test_patch_api_rejects_client_controlled_or_empty_fields(monkeypatch, tmp_path):
    _, service, stylist = _setup(monkeypatch, tmp_path)
    created = _create(service, stylist["id"], "api-owner", _future_start())

    with TestClient(create_app()) as client:
        empty = client.patch(
            f"/api/appointment/{created.appointment_id}",
            json={"user_id": "api-owner", "expected_version": 1},
        )
        controlled = client.patch(
            f"/api/appointment/{created.appointment_id}",
            json={
                "user_id": "api-owner",
                "expected_version": 1,
                "price": 1,
                "duration": 1,
                "end_time": "2030-01-01T00:00:00",
                "status": "cancelled",
                "version": 99,
            },
        )

    assert empty.status_code == 422
    assert controlled.status_code == 422


def test_lifecycle_openapi_has_versioned_request_and_response_schemas(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert "get" in paths["/api/appointment"]
    assert "get" in paths["/api/appointment/{appointment_id}"]
    assert "patch" in paths["/api/appointment/{appointment_id}"]
    assert "post" in paths["/api/appointment/{appointment_id}/cancel"]
    cancel_schema = schema["components"]["schemas"]["AppointmentCancelRequest"]
    update_schema = schema["components"]["schemas"]["AppointmentUpdateRequest"]
    item_schema = schema["components"]["schemas"]["AppointmentLifecycleItem"]
    operation_schema = schema["components"]["schemas"]["AppointmentOperationData"]
    assert {"user_id", "expected_version"}.issubset(cancel_schema["required"])
    assert {"user_id", "expected_version"}.issubset(update_schema["required"])
    assert "version" in item_schema["required"]
    assert "status" in operation_schema["required"]
    assert "exception" not in operation_schema["properties"]
    assert "sql" not in operation_schema["properties"]

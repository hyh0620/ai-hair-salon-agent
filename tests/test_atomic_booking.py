import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from agents.appointment.appointment_database import AppointmentDatabase
from app import create_app
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.availability_service import AvailabilitySearchRequest, AvailabilityService
from services.stylist_service import StylistService


BOOKING_START = datetime(2030, 7, 18, 14, 0)
BOOKING_END = BOOKING_START + timedelta(minutes=45)
SEARCH_NOW = datetime(2030, 7, 17, 12, 0, tzinfo=time_config.BEIJING_TZ)


def _database_counts(db_file):
    with sqlite3.connect(db_file) as connection:
        appointments = connection.execute("select count(*) from appointments").fetchone()[0]
        schedules = connection.execute("select count(*) from stylist_schedules").fetchone()[0]
        linked = connection.execute(
            """
            select count(*)
            from appointments a
            join stylist_schedules s on s.appointment_id = a.id
            """
        ).fetchone()[0]
    return appointments, schedules, linked


def _booking_details(service: AppointmentService, *, user_id="atomic-user"):
    return service.build_appointment_details({
        "user_id": user_id,
        "project": "男士短发",
        "start_time": "2030-07-18 14:00",
        "duration": "45分钟",
    })


def test_concurrent_same_slot_allows_exactly_one_atomic_booking(tmp_path, caplog):
    db_file = tmp_path / "concurrent.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    services = [AppointmentService(db_url), AppointmentService(db_url)]
    stylist_id = services[0].get_stylist_by_name("林浩")["id"]
    barrier = Barrier(2)
    caplog.set_level(logging.INFO, logger="services.appointment_service")

    def book(index):
        barrier.wait()
        return services[index].save_appointment_detailed(
            str(stylist_id),
            BOOKING_START,
            BOOKING_END,
            _booking_details(services[index], user_id=f"concurrent-{index}"),
            f"concurrent-session-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(book, (0, 1)))

    assert sum(result.success for result in results) == 1
    assert [result.reason for result in results if not result.success] == ["schedule_conflict"]
    assert len({result.transaction_id for result in results}) == 2
    assert _database_counts(db_file) == (1, 1, 1)
    messages = [record.getMessage() for record in caplog.records]
    assert any("booking_transaction_commit" in message and "commit=true" in message for message in messages)
    assert any(
        "booking_transaction_rollback" in message
        and "conflict=True" in message
        and "reason=schedule_conflict" in message
        for message in messages
    )


def test_concurrent_different_stylists_succeed_with_distinct_appointment_ids(tmp_path):
    db_file = tmp_path / "concurrent_different_stylists.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    services = [AppointmentService(db_url), AppointmentService(db_url)]
    stylist_ids = [
        services[0].get_stylist_by_name("林浩")["id"],
        services[1].get_stylist_by_name("陈宇")["id"],
    ]
    barrier = Barrier(2)

    def book(index):
        barrier.wait()
        return services[index].save_appointment_detailed(
            str(stylist_ids[index]),
            BOOKING_START,
            BOOKING_END,
            _booking_details(services[index], user_id=f"different-{index}"),
            f"different-session-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(book, (0, 1)))

    assert all(result.success for result in results)
    assert len({result.appointment_id for result in results}) == 2
    assert _database_counts(db_file) == (2, 2, 2)


def test_unassigned_api_returns_same_availability_candidates_without_writing(monkeypatch, tmp_path):
    db_file = tmp_path / "api_candidates.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    payload = {
        "user_id": "api-candidate-user",
        "project": "男士短发",
        "start_time": "2030-07-18 14:00",
        "duration": "45分钟",
    }

    with TestClient(create_app()) as client:
        candidate_response = client.post("/api/appointment/create", json=payload)

        assert candidate_response.status_code == 200
        candidate_data = candidate_response.json()["data"]
        assert candidate_data["status"] == "selection_required"
        assert candidate_data["requires_selection"] is True
        assert candidate_data["requires_confirmation"] is True
        assert candidate_data["candidates"]
        assert set(candidate_data["candidates"][0]) == {
            "option_id",
            "stylist_id",
            "stylist_name",
            "service_key",
            "service_name",
            "specialty_matches",
            "start_time",
            "end_time",
            "duration_minutes",
            "price",
        }
        assert _database_counts(db_file) == (0, 0, 0)

        service = AppointmentService(db_url)
        direct_options = AvailabilityService(service).search_available_stylists(
            AvailabilitySearchRequest(
                target_date=date(2030, 7, 18),
                range_start=time(14, 0),
                range_end=time(14, 0),
                exact_time=time(14, 0),
                service_key="mens_short_cut",
            ),
            now=SEARCH_NOW,
        )
        assert [item["stylist_id"] for item in candidate_data["candidates"]] == [
            item.stylist_id for item in direct_options
        ]

        selected = candidate_data["candidates"][0]
        confirmed = client.post(
            "/api/appointment/create",
            json=payload | {"stylist_id": selected["stylist_id"]},
        )

    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["appointment_id"] is not None
    assert confirmed.json()["data"]["stylist_id"] == selected["stylist_id"]
    assert _database_counts(db_file) == (1, 1, 1)


def test_appointment_openapi_uses_discriminated_response_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'openapi.db'}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")

    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json").json()

    response_schema = schema["components"]["schemas"]["AppointmentCreateResponse"]
    data_schema = response_schema["properties"]["data"]
    assert data_schema["discriminator"]["propertyName"] == "status"
    assert {item["$ref"].rsplit("/", 1)[-1] for item in data_schema["oneOf"]} == {
        "AppointmentResponse",
        "AppointmentSelectionResponse",
    }
    assert schema["paths"]["/api/appointment/create"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AppointmentCreateResponse"
    }


def test_service_capability_filters_candidates_and_blocks_direct_write(tmp_path):
    db_file = tmp_path / "capability.db"
    db_url = f"sqlite:///{db_file}"
    service = AppointmentService(db_url)
    color_only = service.add_stylist("染发老师", "女", "染发调色、冷棕色")
    cut_only = service.add_stylist("剪发老师", "男", "男士短发、渐变推剪")
    request = AvailabilitySearchRequest(
        target_date=date(2030, 7, 18),
        range_start=time(14),
        range_end=time(14),
        exact_time=time(14),
        service_key="mens_short_cut",
    )

    options = AvailabilityService(service).search_available_stylists(
        request,
        now=SEARCH_NOW,
    )

    assert {option.stylist_id for option in options} == {cut_only}
    rejected = service.save_appointment_detailed(
        str(color_only),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service),
        "unsupported-service-session",
    )
    assert rejected.success is False
    assert rejected.reason == "stylist_service_unsupported"
    assert _database_counts(db_file) == (0, 0, 0)


def test_atomic_entry_and_api_reject_past_appointments(monkeypatch, tmp_path):
    direct_db_file = tmp_path / "past_direct.db"
    direct_url = f"sqlite:///{direct_db_file}"
    StylistService(direct_url).initialize_default_stylists()
    service = AppointmentService(direct_url)
    stylist_id = service.get_stylist_by_name("林浩")["id"]
    past_start = datetime(2020, 7, 18, 14, 0)
    direct = service.save_appointment_detailed(
        str(stylist_id),
        past_start,
        past_start + timedelta(minutes=45),
        _booking_details(service),
        "past-chat-session",
    )

    assert direct.success is False
    assert direct.reason == "past_appointment"
    assert _database_counts(direct_db_file) == (0, 0, 0)

    api_db_file = tmp_path / "past_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{api_db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/appointment/create",
            json={
                "project": "男士短发",
                "start_time": "2020-07-18 14:00",
                "duration": "45分钟",
                "stylist_name": "林浩",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "预约时间已经过去"
    assert _database_counts(api_db_file) == (0, 0, 0)


def test_api_without_user_id_uses_request_scoped_tracking_identifier(monkeypatch, tmp_path):
    db_file = tmp_path / "api_tracking.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/appointment/create",
            json={
                "project": "男士短发",
                "start_time": "2030-07-18 14:00",
                "duration": "45分钟",
                "stylist_name": "林浩",
            },
        )

    assert response.status_code == 200
    tracking_user_id = response.json()["data"]["user_id"]
    assert tracking_user_id.startswith("api-session-")
    with sqlite3.connect(db_file) as connection:
        persisted = connection.execute(
            "select user_id, session_id from appointments"
        ).fetchone()
    assert persisted == (tracking_user_id, tracking_user_id)


def test_api_rejects_selected_stylist_without_service_capability(monkeypatch, tmp_path):
    db_file = tmp_path / "api_capability.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    payload = {
        "user_id": "api-capability-user",
        "project": "染发",
        "start_time": "2030-07-18 14:00",
        "duration": "150分钟",
        "stylist_id": 1,
    }

    with TestClient(create_app()) as client:
        response = client.post("/api/appointment/create", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"] == "指定发型师不支持所选服务"
    assert _database_counts(db_file) == (0, 0, 0)


def test_chat_persistence_uses_session_tracking_id_and_blocks_unsupported_service(tmp_path):
    db_file = tmp_path / "chat_tracking.db"
    db_url = f"sqlite:///{db_file}"
    service = AppointmentService(db_url)
    stylist_id = service.add_stylist("染发老师", "女", "染发调色、冷棕色")
    behavior_calls = []

    class RecordingBehaviorService:
        def record_behavior(self, **kwargs):
            behavior_calls.append(kwargs)
            return True

    adapter = AppointmentDatabase(service, RecordingBehaviorService())
    unsupported = adapter.save_appointment_detailed(
        str(stylist_id),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service, user_id=None),
        "chat-session-unsupported",
    )
    assert unsupported.success is False
    assert unsupported.reason == "stylist_service_unsupported"
    assert behavior_calls == []

    cut_stylist_id = service.add_stylist("剪发老师", "男", "男士短发、渐变推剪")
    saved = adapter.save_appointment_detailed(
        str(cut_stylist_id),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service, user_id=None),
        "chat-session-tracking",
    )

    assert saved.success is True
    with sqlite3.connect(db_file) as connection:
        persisted_user_id = connection.execute(
            "select user_id from appointments where id = ?",
            (saved.appointment_id,),
        ).fetchone()[0]
    assert persisted_user_id == "chat-session-tracking"
    assert behavior_calls[0]["user_id"] == "chat-session-tracking"


def test_stale_candidate_is_rechecked_inside_final_transaction(tmp_path):
    db_file = tmp_path / "stale_candidate.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    service = AppointmentService(db_url)
    option = AvailabilityService(service).search_available_stylists(
        AvailabilitySearchRequest(
            target_date=date(2030, 7, 18),
            range_start=time(14),
            range_end=time(14),
            exact_time=time(14),
            service_key="mens_short_cut",
        ),
        now=SEARCH_NOW,
    )[0]

    first = service.save_appointment_detailed(
        str(option.stylist_id),
        option.start_time,
        option.end_time,
        _booking_details(service, user_id="first-user"),
        "first-session",
    )
    stale_confirmation = service.save_appointment_detailed(
        str(option.stylist_id),
        option.start_time,
        option.end_time,
        _booking_details(service, user_id="stale-user"),
        "stale-session",
    )

    assert first.success is True
    assert stale_confirmation.success is False
    assert stale_confirmation.reason == "schedule_conflict"
    assert _database_counts(db_file) == (1, 1, 1)


def test_schedule_write_exception_rolls_back_appointment_row(monkeypatch, tmp_path):
    db_file = tmp_path / "rollback.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    service = AppointmentService(db_url)
    stylist_id = service.get_stylist_by_name("林浩")["id"]

    def fail_schedule_write(_session, **_kwargs):
        raise RuntimeError("injected schedule write failure")

    monkeypatch.setattr(service.stylist_repo, "add_schedule_in_session", fail_schedule_write)
    result = service.save_appointment_detailed(
        str(stylist_id),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service),
        "rollback-session",
    )

    assert result.success is False
    assert result.reason == "persistence_error"
    assert _database_counts(db_file) == (0, 0, 0)


def test_commit_exception_after_both_writes_rolls_back_both_tables(monkeypatch, tmp_path):
    db_file = tmp_path / "commit_rollback.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    service = AppointmentService(db_url)
    stylist_id = service.get_stylist_by_name("林浩")["id"]
    schedule_writes = []
    original_add_schedule = service.stylist_repo.add_schedule_in_session

    def tracked_schedule_write(session, **kwargs):
        schedule_id = original_add_schedule(session, **kwargs)
        schedule_writes.append(schedule_id)
        return schedule_id

    session_class = service.db_router.session_manager.Session.session_factory.class_

    def fail_commit(_session):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(service.stylist_repo, "add_schedule_in_session", tracked_schedule_write)
    monkeypatch.setattr(session_class, "commit", fail_commit)
    result = service.save_appointment_detailed(
        str(stylist_id),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service),
        "commit-rollback-session",
    )

    assert schedule_writes
    assert result.success is False
    assert result.reason == "persistence_error"
    assert _database_counts(db_file) == (0, 0, 0)


def test_transaction_start_exception_returns_failure_without_writes(monkeypatch, tmp_path):
    db_file = tmp_path / "transaction_error.db"
    db_url = f"sqlite:///{db_file}"
    StylistService(db_url).initialize_default_stylists()
    service = AppointmentService(db_url)
    stylist_id = service.get_stylist_by_name("林浩")["id"]

    @contextmanager
    def failed_transaction(*, immediate=False):
        assert immediate is True
        raise RuntimeError("injected transaction start failure")
        yield

    monkeypatch.setattr(service.db_router.session_manager, "session_scope", failed_transaction)
    result = service.save_appointment_detailed(
        str(stylist_id),
        BOOKING_START,
        BOOKING_END,
        _booking_details(service),
        "transaction-error-session",
    )

    assert result.success is False
    assert result.reason == "persistence_error"
    assert _database_counts(db_file) == (0, 0, 0)


def test_database_trigger_allows_adjacent_and_different_stylist_schedules(tmp_path):
    db_file = tmp_path / "trigger_allowed.db"
    db_url = f"sqlite:///{db_file}"
    service = AppointmentService(db_url)
    first_stylist = service.add_stylist("林浩", "男", "男士短发、渐变推剪")
    second_stylist = service.add_stylist("陈宇", "男", "男士短发、渐变推剪")

    first_id = service.stylist_repo.add_schedule(
        first_stylist,
        BOOKING_START,
        BOOKING_END,
        "busy",
        9001,
    )
    adjacent_id = service.stylist_repo.add_schedule(
        first_stylist,
        BOOKING_END,
        BOOKING_END + timedelta(minutes=45),
        "busy",
        9002,
    )
    other_stylist_id = service.stylist_repo.add_schedule(
        second_stylist,
        BOOKING_START,
        BOOKING_END,
        "busy",
        9003,
    )

    assert len({first_id, adjacent_id, other_stylist_id}) == 3


def test_database_insert_and_update_triggers_reject_overlapping_busy_schedule(tmp_path):
    db_file = tmp_path / "trigger.db"
    db_url = f"sqlite:///{db_file}"
    service = AppointmentService(db_url)
    stylist_id = service.add_stylist("林浩", "男", "男士短发、渐变推剪")
    service.stylist_repo.add_schedule(
        stylist_id,
        BOOKING_START,
        BOOKING_END,
        "busy",
        9001,
    )

    with pytest.raises(IntegrityError, match="schedule_conflict"):
        service.stylist_repo.add_schedule(
            stylist_id,
            BOOKING_START + timedelta(minutes=15),
            BOOKING_END + timedelta(minutes=15),
            "busy",
            9002,
        )

    free_schedule_id = service.stylist_repo.add_schedule(
        stylist_id,
        BOOKING_START + timedelta(minutes=15),
        BOOKING_END + timedelta(minutes=15),
        "free",
        None,
    )
    with pytest.raises(IntegrityError, match="schedule_conflict"):
        service.stylist_repo.update_schedule_status(free_schedule_id, "busy", 9003)


def test_trigger_integrity_error_maps_to_stable_conflict_reason(monkeypatch, tmp_path):
    db_file = tmp_path / "trigger_mapping.db"
    db_url = f"sqlite:///{db_file}"
    service = AppointmentService(db_url)
    stylist_id = service.add_stylist("林浩", "男", "男士短发、渐变推剪")
    service.stylist_repo.add_schedule(
        stylist_id,
        BOOKING_START,
        BOOKING_END,
        "busy",
        9001,
    )
    monkeypatch.setattr(
        service.stylist_repo,
        "has_schedule_conflict_in_session",
        lambda _session, **_kwargs: False,
    )

    result = service.save_appointment_detailed(
        str(stylist_id),
        BOOKING_START + timedelta(minutes=15),
        BOOKING_END + timedelta(minutes=15),
        _booking_details(service),
        "trigger-mapping-session",
    )

    assert result.success is False
    assert result.reason == "schedule_conflict"
    assert _database_counts(db_file) == (0, 1, 0)


def test_legacy_database_upgrade_is_idempotent_and_preserves_existing_rows(tmp_path):
    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as connection:
        connection.executescript(
            """
            CREATE TABLE stylists (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR UNIQUE,
                gender VARCHAR,
                specialties VARCHAR
            );
            CREATE TABLE stylist_schedules (
                id INTEGER NOT NULL PRIMARY KEY,
                stylist_id INTEGER,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status VARCHAR NOT NULL,
                appointment_id INTEGER,
                FOREIGN KEY(stylist_id) REFERENCES stylists (id)
            );
            INSERT INTO stylists (id, name, gender, specialties)
            VALUES (42, '历史发型师', '男', '男士短发');
            INSERT INTO stylist_schedules (
                id, stylist_id, start_time, end_time, status, appointment_id
            ) VALUES (
                77, 42, '2029-01-01 14:00:00.000000',
                '2029-01-01 14:45:00.000000', 'busy', 7001
            );
            """
        )

    first = AppointmentService(f"sqlite:///{db_file}")
    second = AppointmentService(f"sqlite:///{db_file}")

    with sqlite3.connect(db_file) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type = 'table'")
        }
        triggers = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type = 'trigger'")
        }
        stylist = connection.execute(
            "select id, name from stylists where id = 42"
        ).fetchone()
        schedule = connection.execute(
            "select id, stylist_id, appointment_id from stylist_schedules where id = 77"
        ).fetchone()

    assert "appointments" in tables
    assert triggers == {
        "prevent_overlapping_busy_schedule_insert",
        "prevent_overlapping_busy_schedule_update",
    }
    assert stylist == (42, "历史发型师")
    assert schedule == (77, 42, 7001)
    first.db_router.close()
    second.db_router.close()

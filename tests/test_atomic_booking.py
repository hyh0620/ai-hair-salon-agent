import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

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


def test_database_trigger_rejects_overlapping_busy_schedule(tmp_path):
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

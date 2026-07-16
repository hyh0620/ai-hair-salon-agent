import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier

import pytest

from config.time_config import time_config
from services.appointment_service import AppointmentService


def _future_start(*, days=30, hour=14, minute=0):
    return (time_config.now() + timedelta(days=days)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
        tzinfo=None,
    )


def _service(tmp_path, name="lifecycle.db"):
    db_file = tmp_path / name
    service = AppointmentService(f"sqlite:///{db_file}")
    flexible = service.add_stylist(
        "全能老师",
        "女",
        "男士短发、渐变推剪、染发调色、冷棕色、挑染",
    )
    cut_only = service.add_stylist("剪发老师", "男", "男士短发、渐变推剪")
    color_only = service.add_stylist("染发老师", "女", "染发调色、冷棕色、挑染")
    return db_file, service, flexible, cut_only, color_only


def _create(
    service,
    stylist_id,
    *,
    owner="owner-a",
    start=None,
    project="男士短发",
):
    start = start or _future_start()
    details = service.build_appointment_details({"project": project, "user_id": owner})
    duration = details["duration_minutes"]
    result = service.save_appointment_detailed(
        str(stylist_id),
        start,
        start + timedelta(minutes=duration),
        details,
        owner,
    )
    assert result.success, result
    return result


def _appointment_row(db_file, appointment_id):
    with sqlite3.connect(db_file) as connection:
        return connection.execute(
            """
            SELECT id, user_id, stylist_id, service_key, start_time, end_time,
                   status, version, updated_at
            FROM appointments WHERE id = ?
            """,
            (appointment_id,),
        ).fetchone()


def _schedule_rows(db_file, appointment_id):
    with sqlite3.connect(db_file) as connection:
        return connection.execute(
            """
            SELECT id, stylist_id, start_time, end_time, status, appointment_id
            FROM stylist_schedules WHERE appointment_id = ? ORDER BY id
            """,
            (appointment_id,),
        ).fetchall()


def _assert_lifecycle_invariants(db_file):
    with sqlite3.connect(db_file) as connection:
        confirmed_without_one_busy = connection.execute(
            """
            SELECT COUNT(*) FROM appointments a
            WHERE a.status = 'confirmed'
              AND (SELECT COUNT(*) FROM stylist_schedules s
                   WHERE s.appointment_id = a.id AND s.status = 'busy') != 1
            """
        ).fetchone()[0]
        cancelled_with_busy = connection.execute(
            """
            SELECT COUNT(*) FROM appointments a
            JOIN stylist_schedules s ON s.appointment_id = a.id
            WHERE a.status = 'cancelled' AND s.status = 'busy'
            """
        ).fetchone()[0]
        orphan_busy = connection.execute(
            """
            SELECT COUNT(*) FROM stylist_schedules s
            LEFT JOIN appointments a ON a.id = s.appointment_id
            WHERE s.status = 'busy' AND a.id IS NULL
            """
        ).fetchone()[0]
    assert (confirmed_without_one_busy, cancelled_with_busy, orphan_busy) == (0, 0, 0)


def test_owner_scoped_query_sorting_filters_and_hidden_ownership(tmp_path):
    _, service, flexible, _, _ = _service(tmp_path)
    later = _create(service, flexible, owner="owner-a", start=_future_start(days=32))
    earlier = _create(service, flexible, owner="owner-a", start=_future_start(days=31))
    _create(service, flexible, owner="owner-b", start=_future_start(days=30))

    listed = service.list_user_appointments("owner-a")
    by_date = service.list_user_appointments(
        "owner-a",
        target_date=_future_start(days=31).date(),
    )
    hidden = service.get_user_appointment(earlier.appointment_id, "owner-b")

    assert [item["appointment_id"] for item in listed.appointments] == [
        earlier.appointment_id,
        later.appointment_id,
    ]
    assert [item["appointment_id"] for item in by_date.appointments] == [
        earlier.appointment_id
    ]
    assert hidden.status == "not_found"
    assert hidden.internal_reason == "ownership_mismatch"


def test_cancel_is_atomic_versioned_and_releases_slot(tmp_path):
    db_file, service, flexible, _, _ = _service(tmp_path)
    start = _future_start()
    created = _create(service, flexible, start=start)

    cancelled = service.cancel_appointment(created.appointment_id, "owner-a", 1)
    repeated = service.cancel_appointment(created.appointment_id, "owner-a", 1)
    replacement = _create(service, flexible, owner="owner-b", start=start)

    assert cancelled.status == "success"
    assert cancelled.current_version == 2
    assert repeated.status == "already_cancelled"
    assert replacement.success
    assert _appointment_row(db_file, created.appointment_id)[6:8] == ("cancelled", 2)
    assert _schedule_rows(db_file, created.appointment_id)[0][4] == "cancelled"
    _assert_lifecycle_invariants(db_file)


@pytest.mark.parametrize("status_value", ["cancelled", "completed"])
def test_cancel_rejects_non_modifiable_status(tmp_path, status_value):
    db_file, service, flexible, _, _ = _service(tmp_path, f"cancel-{status_value}.db")
    created = _create(service, flexible)
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ?",
            (status_value, created.appointment_id),
        )
        if status_value == "cancelled":
            connection.execute(
                "UPDATE stylist_schedules SET status = 'cancelled' WHERE appointment_id = ?",
                (created.appointment_id,),
            )

    result = service.cancel_appointment(created.appointment_id, "owner-a", 1)

    if status_value == "cancelled":
        assert result.status == "already_cancelled"
    else:
        assert result.status == "not_modifiable"


def test_cancel_rejects_past_and_stale_versions(tmp_path):
    db_file, service, flexible, _, _ = _service(tmp_path)
    stale_created = _create(service, flexible, start=_future_start(days=30))
    past_created = _create(service, flexible, start=_future_start(days=31))
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "UPDATE appointments SET version = 2 WHERE id = ?",
            (stale_created.appointment_id,),
        )
        connection.execute(
            "UPDATE appointments SET start_time = '2020-01-01 14:00:00', "
            "end_time = '2020-01-01 14:45:00' WHERE id = ?",
            (past_created.appointment_id,),
        )
        connection.execute(
            "UPDATE stylist_schedules SET start_time = '2020-01-01 14:00:00', "
            "end_time = '2020-01-01 14:45:00' WHERE appointment_id = ?",
            (past_created.appointment_id,),
        )

    stale = service.cancel_appointment(stale_created.appointment_id, "owner-a", 1)
    past = service.cancel_appointment(past_created.appointment_id, "owner-a", 1)

    assert stale.status == "stale_state"
    assert stale.current_version == 2
    assert past.status == "not_modifiable"
    assert past.reason == "past_appointment"


def test_cancel_schedule_failure_rolls_back_appointment(monkeypatch, tmp_path):
    db_file, service, flexible, _, _ = _service(tmp_path)
    created = _create(service, flexible)

    def fail_schedule(*_args, **_kwargs):
        raise RuntimeError("injected schedule failure")

    monkeypatch.setattr(service.stylist_repo, "cancel_schedule_in_session", fail_schedule)
    result = service.cancel_appointment(created.appointment_id, "owner-a", 1)

    assert result.status == "persistence_error"
    assert _appointment_row(db_file, created.appointment_id)[6:8] == ("confirmed", 1)
    assert _schedule_rows(db_file, created.appointment_id)[0][4] == "busy"


def test_update_partial_fields_recalculates_catalog_and_preserves_id(tmp_path):
    db_file, service, flexible, _, color_only = _service(tmp_path)
    created = _create(service, flexible)
    target_date = _future_start(days=40).date()

    changed = service.update_appointment(
        created.appointment_id,
        "owner-a",
        1,
        target_date=target_date,
        target_time=time(15, 30),
        stylist_id=color_only,
        service_value="染发",
    )

    assert changed.status == "success"
    assert changed.appointment["appointment_id"] == created.appointment_id
    assert changed.appointment["service_key"] == "color"
    assert changed.appointment["duration_minutes"] == 150
    assert changed.appointment["price"] == 398
    assert changed.appointment["end_time"] - changed.appointment["start_time"] == timedelta(minutes=150)
    assert changed.current_version == 2
    row = _appointment_row(db_file, created.appointment_id)
    schedule = _schedule_rows(db_file, created.appointment_id)
    assert row[0] == created.appointment_id
    assert row[2] == color_only
    assert row[3] == "color"
    assert len(schedule) == 1
    assert schedule[0][1] == color_only
    assert schedule[0][5] == created.appointment_id
    _assert_lifecycle_invariants(db_file)


def test_update_date_only_and_time_only_patch_semantics(tmp_path):
    _, service, flexible, _, _ = _service(tmp_path)
    start = _future_start(days=30, hour=14, minute=30)
    first = _create(service, flexible, start=start)

    date_changed = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        target_date=_future_start(days=35).date(),
    )
    time_changed = service.update_appointment(
        first.appointment_id,
        "owner-a",
        2,
        target_time=time(16, 0),
    )

    assert date_changed.appointment["start_time"].time() == time(14, 30)
    assert time_changed.appointment["start_time"].date() == _future_start(days=35).date()
    assert time_changed.appointment["start_time"].time() == time(16, 0)


def test_update_rejects_conflict_unsupported_past_and_outside_hours(tmp_path):
    db_file, service, flexible, cut_only, color_only = _service(tmp_path)
    first = _create(service, flexible, start=_future_start(days=30, hour=14))
    _create(service, cut_only, owner="owner-b", start=_future_start(days=30, hour=16))

    conflict = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        stylist_id=cut_only,
        target_time=time(16),
    )
    unsupported = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        stylist_id=color_only,
    )
    past = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        target_date=datetime(2020, 1, 1).date(),
    )
    outside = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        target_time=time(23),
    )

    assert conflict.status == "conflict"
    assert unsupported.status == "service_not_supported"
    assert past.status == "invalid_time"
    assert outside.status == "outside_business_hours"
    assert _appointment_row(db_file, first.appointment_id)[6:8] == ("confirmed", 1)
    assert _schedule_rows(db_file, first.appointment_id)[0][4] == "busy"


def test_update_excludes_own_schedule_and_no_change_does_not_increment(tmp_path):
    db_file, service, flexible, _, _ = _service(tmp_path)
    created = _create(service, flexible)

    no_change = service.update_appointment(
        created.appointment_id,
        "owner-a",
        1,
        stylist_id=flexible,
        service_value="男士短发",
    )

    assert no_change.status == "no_change"
    assert no_change.current_version == 1
    assert _appointment_row(db_file, created.appointment_id)[7] == 1


def test_update_schedule_failure_rolls_back_both_rows(monkeypatch, tmp_path):
    db_file, service, flexible, _, _ = _service(tmp_path)
    created = _create(service, flexible)
    original_appointment = _appointment_row(db_file, created.appointment_id)
    original_schedule = _schedule_rows(db_file, created.appointment_id)

    def fail_update(*_args, **_kwargs):
        raise RuntimeError("injected update failure")

    monkeypatch.setattr(service.stylist_repo, "update_schedule_in_session", fail_update)
    result = service.update_appointment(
        created.appointment_id,
        "owner-a",
        1,
        target_time=time(16),
    )

    assert result.status == "persistence_error"
    assert _appointment_row(db_file, created.appointment_id) == original_appointment
    assert _schedule_rows(db_file, created.appointment_id) == original_schedule


def test_update_trigger_error_maps_to_conflict_and_preserves_original(monkeypatch, tmp_path):
    db_file, service, flexible, cut_only, _ = _service(tmp_path)
    first = _create(service, flexible, start=_future_start(days=30, hour=14))
    _create(service, cut_only, owner="owner-b", start=_future_start(days=30, hour=16))
    monkeypatch.setattr(
        service.stylist_repo,
        "has_schedule_conflict_in_session",
        lambda *_args, **_kwargs: False,
    )

    result = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        stylist_id=cut_only,
        target_time=time(16),
    )

    assert result.status == "conflict"
    assert _appointment_row(db_file, first.appointment_id)[2] == flexible
    assert _appointment_row(db_file, first.appointment_id)[7] == 1
    assert _schedule_rows(db_file, first.appointment_id)[0][1] == flexible


def test_concurrent_same_version_updates_allow_only_one_commit(tmp_path):
    db_file, setup_service, flexible, _, _ = _service(tmp_path)
    created = _create(setup_service, flexible)
    url = f"sqlite:///{db_file}"
    services = [AppointmentService(url), AppointmentService(url)]
    barrier = Barrier(2)

    def update(index):
        barrier.wait()
        return services[index].update_appointment(
            created.appointment_id,
            "owner-a",
            1,
            target_time=(time(15), time(16))[index],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, (0, 1)))

    assert sum(result.status == "success" for result in results) == 1
    assert sum(result.status == "stale_state" for result in results) == 1
    assert _appointment_row(db_file, created.appointment_id)[7] == 2
    _assert_lifecycle_invariants(db_file)


def test_concurrent_two_appointments_rescheduling_to_one_slot_preserves_loser(tmp_path):
    db_file, setup_service, flexible, cut_only, _ = _service(tmp_path)
    first = _create(setup_service, flexible, start=_future_start(days=30, hour=14))
    second = _create(setup_service, cut_only, owner="owner-b", start=_future_start(days=30, hour=17))
    target_date = _future_start(days=31).date()
    url = f"sqlite:///{db_file}"
    services = [AppointmentService(url), AppointmentService(url)]
    barrier = Barrier(2)

    def update(index):
        barrier.wait()
        appointment_id, owner, stylist = (
            (first.appointment_id, "owner-a", flexible),
            (second.appointment_id, "owner-b", flexible),
        )[index]
        return services[index].update_appointment(
            appointment_id,
            owner,
            1,
            target_date=target_date,
            target_time=time(15),
            stylist_id=stylist,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, (0, 1)))

    assert sum(result.status == "success" for result in results) == 1
    assert sum(result.status == "conflict" for result in results) == 1
    loser = first if results[0].status == "conflict" else second
    loser_row = _appointment_row(db_file, loser.appointment_id)
    assert loser_row[7] == 1
    _assert_lifecycle_invariants(db_file)


def test_concurrent_update_and_cancel_same_version_are_serialized(tmp_path):
    db_file, setup_service, flexible, _, _ = _service(tmp_path)
    created = _create(setup_service, flexible)
    url = f"sqlite:///{db_file}"
    update_service = AppointmentService(url)
    cancel_service = AppointmentService(url)
    barrier = Barrier(2)

    def update():
        barrier.wait()
        return update_service.update_appointment(
            created.appointment_id,
            "owner-a",
            1,
            target_time=time(16),
        )

    def cancel():
        barrier.wait()
        return cancel_service.cancel_appointment(created.appointment_id, "owner-a", 1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        update_future = executor.submit(update)
        cancel_future = executor.submit(cancel)
        results = [update_future.result(), cancel_future.result()]

    assert sum(result.status == "success" for result in results) == 1
    assert results[0].status in {"success", "stale_state", "not_modifiable"}
    assert results[1].status in {"success", "stale_state", "already_cancelled"}
    _assert_lifecycle_invariants(db_file)


def test_concurrent_cancel_and_new_booking_preserve_slot_invariants(tmp_path):
    db_file, setup_service, flexible, _, _ = _service(tmp_path)
    start = _future_start()
    created = _create(setup_service, flexible, start=start)
    url = f"sqlite:///{db_file}"
    cancel_service = AppointmentService(url)
    booking_service = AppointmentService(url)
    barrier = Barrier(2)

    def cancel():
        barrier.wait()
        return cancel_service.cancel_appointment(created.appointment_id, "owner-a", 1)

    def book():
        barrier.wait()
        details = booking_service.build_appointment_details({
            "project": "男士短发",
            "user_id": "owner-b",
        })
        return booking_service.save_appointment_detailed(
            str(flexible),
            start,
            start + timedelta(minutes=45),
            details,
            "owner-b",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(cancel)
        booking_future = executor.submit(book)
        cancelled, booked = cancel_future.result(), booking_future.result()

    assert cancelled.status == "success"
    assert booked.success or booked.reason == "schedule_conflict"
    _assert_lifecycle_invariants(db_file)
    with sqlite3.connect(db_file) as connection:
        busy_count = connection.execute(
            "SELECT COUNT(*) FROM stylist_schedules WHERE status='busy'"
        ).fetchone()[0]
    assert busy_count in {0, 1}


@pytest.mark.parametrize("operation", ["cancel", "update"])
def test_lifecycle_commit_exception_rolls_back_all_changes(monkeypatch, tmp_path, operation):
    db_file, service, flexible, _, _ = _service(tmp_path, f"commit-{operation}.db")
    created = _create(service, flexible)
    original_appointment = _appointment_row(db_file, created.appointment_id)
    original_schedule = _schedule_rows(db_file, created.appointment_id)
    session_class = service.db_router.session_manager.Session.session_factory.class_

    def fail_commit(_session):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(session_class, "commit", fail_commit)
    if operation == "cancel":
        result = service.cancel_appointment(created.appointment_id, "owner-a", 1)
    else:
        result = service.update_appointment(
            created.appointment_id,
            "owner-a",
            1,
            target_time=time(16),
        )

    assert result.status == "persistence_error"
    assert _appointment_row(db_file, created.appointment_id) == original_appointment
    assert _schedule_rows(db_file, created.appointment_id) == original_schedule


def test_update_to_adjacent_slot_is_allowed(tmp_path):
    db_file, service, flexible, cut_only, _ = _service(tmp_path)
    first = _create(service, flexible, start=_future_start(days=30, hour=14))
    _create(service, cut_only, owner="owner-b", start=_future_start(days=30, hour=15))

    changed = service.update_appointment(
        first.appointment_id,
        "owner-a",
        1,
        stylist_id=cut_only,
        target_time=time(14, 15),
    )

    assert changed.status == "success"
    assert changed.appointment["end_time"].time() == time(15)
    _assert_lifecycle_invariants(db_file)


def test_legacy_appointments_columns_upgrade_is_repeatable_and_preserves_rows(tmp_path):
    db_file = tmp_path / "legacy-lifecycle.db"
    with sqlite3.connect(db_file) as connection:
        connection.executescript(
            """
            CREATE TABLE stylists (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR UNIQUE,
                gender VARCHAR,
                specialties VARCHAR
            );
            CREATE TABLE appointments (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR NOT NULL,
                session_id VARCHAR,
                stylist_id INTEGER NOT NULL,
                service_key VARCHAR NOT NULL,
                service_name VARCHAR NOT NULL,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                duration_minutes INTEGER NOT NULL,
                price INTEGER NOT NULL,
                notes TEXT,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE stylist_schedules (
                id INTEGER NOT NULL PRIMARY KEY,
                stylist_id INTEGER,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status VARCHAR NOT NULL,
                appointment_id INTEGER
            );
            INSERT INTO stylists VALUES (1, '历史老师', '男', '男士短发');
            INSERT INTO appointments (
                id, user_id, session_id, stylist_id, service_key, service_name,
                start_time, end_time, duration_minutes, price, notes, created_at
            ) VALUES (
                9, 'legacy-owner', 'legacy-session', 1, 'mens_short_cut', '男士短发',
                '2035-01-02 14:00:00', '2035-01-02 14:45:00', 45, 88, NULL,
                '2030-01-01 10:00:00'
            );
            INSERT INTO stylist_schedules VALUES (
                10, 1, '2035-01-02 14:00:00', '2035-01-02 14:45:00', 'busy', 9
            );
            """
        )

    first = AppointmentService(f"sqlite:///{db_file}")
    second = AppointmentService(f"sqlite:///{db_file}")

    with sqlite3.connect(db_file) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(appointments)")
        }
        row = connection.execute(
            "SELECT id, status, version, start_time FROM appointments WHERE id = 9"
        ).fetchone()
        schedule = connection.execute(
            "SELECT id, status, appointment_id FROM stylist_schedules WHERE id = 10"
        ).fetchone()
        triggers = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0]

    assert {"status", "updated_at", "version"}.issubset(columns)
    assert row == (9, "confirmed", 1, "2035-01-02 14:00:00")
    assert schedule == (10, "busy", 9)
    assert triggers == 2
    first.db_router.close()
    second.db_router.close()

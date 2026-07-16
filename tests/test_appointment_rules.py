from datetime import datetime, timedelta

import pytest

from agents.appointment.stylist_finder import StylistFinder
from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService


@pytest.fixture()
def salon_stack(tmp_path, monkeypatch):
    monkeypatch.setattr(
        time_config,
        "now",
        lambda: datetime(2026, 7, 5, 9, 0, tzinfo=time_config.BEIJING_TZ),
    )
    db_path = f"sqlite:///{tmp_path / 'salon.db'}"
    StylistService(db_path).initialize_default_stylists()
    appointment_service = AppointmentService(db_path)
    finder = StylistFinder(appointment_service)
    return appointment_service, finder


def test_cut_appointment_can_be_created_without_gender_preference(salon_stack):
    appointment_service, finder = salon_stack
    history = {
        "project": "男士短发",
        "start_time": "2026-07-06 14:00",
        "duration": "45分钟",
        "style_preference": "清爽渐变",
    }

    details = appointment_service.build_appointment_details(history)
    stylist = finder.find_stylist_with_thought(details)
    start_time, end_time, _ = finder.parse_time_and_duration(
        details["start_time"],
        details["duration"],
    )

    assert stylist is not None
    assert details["project"] == "男士短发"
    assert details["duration"] == "45分钟"
    assert details["price"] == 88
    assert appointment_service.save_appointment(
        str(stylist["id"]),
        start_time,
        end_time,
        details,
        "test-session",
    )


def test_mens_short_cut_recommends_matching_stylist_specialty(salon_stack):
    appointment_service, finder = salon_stack
    ranked = finder.rank_stylists(
        appointment_service.get_all_stylists(),
        {
            "project": "男士短发",
            "start_time": "2026-07-06 15:00",
            "duration": "45分钟",
            "style_preference": "渐变推剪",
        },
    )

    assert ranked
    assert ranked[0]["name"] in {"林浩", "陈宇"}
    assert "男士短发" in ranked[0]["specialties"] or "渐变推剪" in ranked[0]["specialties"]


def test_color_and_perm_return_catalog_duration_and_price(salon_stack):
    appointment_service, _ = salon_stack

    color = appointment_service.build_appointment_details({
        "project": "染发",
        "start_time": "2026-07-06 10:00",
        "duration": "60分钟",
    })
    perm = appointment_service.build_appointment_details({
        "project": "蓬松烫",
        "start_time": "2026-07-06 13:00",
        "duration": "60分钟",
    })

    assert color["project"] == "染发"
    assert color["duration"] == "150分钟"
    assert color["price"] == 398
    assert perm["project"] == "烫发"
    assert perm["duration"] == "180分钟"
    assert perm["price"] == 468


def test_same_stylist_same_slot_is_blocked_by_conflict_check(salon_stack):
    appointment_service, _ = salon_stack
    stylist = appointment_service.get_stylist_by_name("林浩")
    start_time = datetime(2026, 7, 6, 14, 0)
    end_time = start_time + timedelta(minutes=45)
    details = appointment_service.build_appointment_details({
        "project": "男士短发",
        "start_time": "2026-07-06 14:00",
        "duration": "45分钟",
    })

    first = appointment_service.save_appointment(
        str(stylist["id"]),
        start_time,
        end_time,
        details,
        "first-session",
    )
    second = appointment_service.save_appointment(
        str(stylist["id"]),
        start_time,
        end_time,
        details,
        "second-session",
    )

    assert first is True
    assert second is False

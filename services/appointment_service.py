"""Appointment service with deterministic schedule and catalog rules."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from config.time_config import time_config
from db.db_router import DatabaseRouter
from services.service_catalog import (
    normalize_service,
    parse_duration_minutes,
    structured_stylist_profile,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppointmentSaveResult:
    success: bool
    appointment_id: Optional[int] = None
    schedule_id: Optional[int] = None
    reason: Optional[str] = None
    transaction_id: Optional[str] = None


class _BookingRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AppointmentService:
    """Business logic for appointment persistence and stylist availability."""

    def __init__(self, db_path: str = None):
        self.db_router = DatabaseRouter(db_path)
        self.appointment_repo = self.db_router.appointments
        self.stylist_repo = self.db_router.stylists

    def build_appointment_details(self, appointment_history: Dict[str, Any]) -> Dict[str, Any]:
        service = normalize_service(
            appointment_history.get("service_key") or appointment_history.get("project")
        )
        duration = parse_duration_minutes(appointment_history.get("duration"))

        if service:
            duration = service.standard_duration

        details = dict(appointment_history)
        if service:
            details["project"] = service.name
            details["service_key"] = service.key
            details["standard_price"] = service.standard_price
            details["price"] = service.standard_price
            details["catalog_duration"] = service.standard_duration
        if duration is not None:
            details["duration"] = f"{duration}分钟"
            details["duration_minutes"] = duration
        return details

    def save_appointment(
        self,
        stylist_id: str,
        start_time: datetime,
        end_time: datetime,
        appointment_history: Dict[str, Any],
        session_id: str,
    ) -> bool:
        """Compatibility wrapper that preserves the historical bool contract."""
        return self.save_appointment_detailed(
            stylist_id,
            start_time,
            end_time,
            appointment_history,
            session_id,
        ).success

    def save_appointment_detailed(
        self,
        stylist_id: str,
        start_time: datetime,
        end_time: datetime,
        appointment_history: Dict[str, Any],
        session_id: str,
    ) -> AppointmentSaveResult:
        transaction_id = uuid4().hex
        appointment_id = None
        schedule_id = None
        try:
            stylist_id_int = int(stylist_id)
        except (TypeError, ValueError):
            logger.warning(
                "booking_transaction_rollback session_id=%s transaction_id=%s "
                "appointment_id=None stylist_id=%s rollback=true conflict=false "
                "reason=stylist_not_found",
                session_id,
                transaction_id,
                stylist_id,
            )
            return AppointmentSaveResult(
                False,
                reason="stylist_not_found",
                transaction_id=transaction_id,
            )
        logger.info(
            "booking_transaction_begin session_id=%s transaction_id=%s stylist_id=%s",
            session_id,
            transaction_id,
            stylist_id_int,
        )
        try:
            details = self.build_appointment_details(appointment_history)
            service = normalize_service(details.get("service_key") or details.get("project"))

            with self.db_router.session_manager.session_scope(immediate=True) as session:
                if not self.is_future_start_time(start_time):
                    raise _BookingRejected("past_appointment")
                if not self.is_within_business_hours(start_time, end_time):
                    raise _BookingRejected("outside_business_hours")
                if not service:
                    raise _BookingRejected("unknown_service")
                expected_end = start_time + timedelta(minutes=service.standard_duration)
                if end_time != expected_end:
                    raise _BookingRejected("invalid_service_duration")

                stylist = self.stylist_repo.get_stylist_by_id_in_session(session, stylist_id_int)
                if not stylist:
                    raise _BookingRejected("stylist_not_found")
                if not self.stylist_supports_service(stylist, service.key):
                    raise _BookingRejected("stylist_service_unsupported")
                if self.stylist_repo.has_schedule_conflict_in_session(
                    session,
                    stylist_id=stylist_id_int,
                    start_time=start_time,
                    end_time=end_time,
                ):
                    raise _BookingRejected("schedule_conflict")

                appointment_id = self.appointment_repo.add_appointment_in_session(
                    session,
                    # A chat session is a tracking identifier, not an authenticated user ID.
                    user_id=str(details.get("user_id") or session_id),
                    session_id=session_id,
                    stylist_id=stylist_id_int,
                    service_key=service.key,
                    service_name=service.name,
                    start_time=start_time,
                    end_time=end_time,
                    duration_minutes=service.standard_duration,
                    price=service.standard_price,
                    notes=details.get("notes"),
                )
                schedule_id = self.stylist_repo.add_schedule_in_session(
                    session,
                    stylist_id=stylist_id_int,
                    start_time=start_time,
                    end_time=end_time,
                    status="busy",
                    appointment_id=appointment_id,
                )

            logger.info(
                "booking_transaction_commit session_id=%s transaction_id=%s "
                "appointment_id=%s stylist_id=%s schedule_id=%s commit=true",
                session_id,
                transaction_id,
                appointment_id,
                stylist_id_int,
                schedule_id,
            )
            return AppointmentSaveResult(
                success=True,
                appointment_id=appointment_id,
                schedule_id=schedule_id,
                transaction_id=transaction_id,
            )
        except _BookingRejected as exc:
            logger.warning(
                "booking_transaction_rollback session_id=%s transaction_id=%s "
                "appointment_id=%s stylist_id=%s rollback=true conflict=%s reason=%s",
                session_id,
                transaction_id,
                appointment_id,
                stylist_id_int,
                exc.reason == "schedule_conflict",
                exc.reason,
            )
            return AppointmentSaveResult(
                False,
                reason=exc.reason,
                transaction_id=transaction_id,
            )
        except IntegrityError as exc:
            reason = "schedule_conflict" if "schedule_conflict" in str(exc.orig) else "persistence_error"
            logger.warning(
                "booking_transaction_rollback session_id=%s transaction_id=%s "
                "appointment_id=%s stylist_id=%s rollback=true conflict=%s reason=%s",
                session_id,
                transaction_id,
                appointment_id,
                stylist_id_int,
                reason == "schedule_conflict",
                reason,
            )
            return AppointmentSaveResult(False, reason=reason, transaction_id=transaction_id)
        except Exception as exc:
            logger.exception(
                "booking_transaction_rollback session_id=%s transaction_id=%s "
                "appointment_id=%s stylist_id=%s rollback=true conflict=false "
                "reason=persistence_error error_type=%s",
                session_id,
                transaction_id,
                appointment_id,
                stylist_id_int,
                type(exc).__name__,
            )
            return AppointmentSaveResult(
                False,
                reason="persistence_error",
                transaction_id=transaction_id,
            )

    @staticmethod
    def is_future_start_time(start_time: datetime) -> bool:
        now = time_config.now()
        if start_time.tzinfo is None:
            comparable_now = now.astimezone(time_config.BEIJING_TZ).replace(tzinfo=None)
        else:
            comparable_now = now.astimezone(start_time.tzinfo)
        return start_time > comparable_now

    def is_within_business_hours(self, start_time: datetime, end_time: datetime) -> bool:
        start_hour, end_hour = time_config.get_business_hours()
        return (
            start_time.date() == end_time.date()
            and start_hour <= start_time.hour
            and (end_time.hour < end_hour or (end_time.hour == end_hour and end_time.minute == 0))
        )

    def get_stylist_by_id(self, stylist_id: int) -> Optional[Dict[str, Any]]:
        try:
            return self.stylist_repo.get_stylist_by_id(stylist_id)
        except Exception as exc:
            logger.error("获取发型师信息失败: %s", exc)
            return None

    def get_stylist_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            return self.stylist_repo.get_stylist_by_name(name)
        except Exception as exc:
            logger.error("获取发型师信息失败: %s", exc)
            return None

    def get_all_stylists(self) -> List[Dict[str, Any]]:
        try:
            return self.stylist_repo.get_all_stylists()
        except Exception as exc:
            logger.error("获取发型师列表失败: %s", exc)
            return []

    def get_stylists_by_gender(self, gender: str) -> List[Dict[str, Any]]:
        try:
            return self.stylist_repo.get_stylists_by_gender(gender)
        except Exception as exc:
            logger.error("根据性别获取发型师信息失败: %s", exc)
            return []

    def get_stylist_schedules(self, stylist_id: int, date) -> List[Dict[str, Any]]:
        try:
            return self.stylist_repo.get_stylist_schedules(stylist_id, date)
        except Exception as exc:
            logger.error("获取发型师排班信息失败: %s", exc)
            return []

    def is_stylist_available(self, stylist_id: int, start_time: datetime, end_time: datetime) -> bool:
        try:
            return self.stylist_repo.is_stylist_available(stylist_id, start_time, end_time)
        except Exception as exc:
            logger.error("检查发型师可用性失败: %s", exc)
            return False

    @staticmethod
    def stylist_supports_service(stylist: Dict[str, Any], service_value: str) -> bool:
        service = normalize_service(service_value)
        if not service:
            return False
        profile = structured_stylist_profile(stylist)
        return service.key in profile.get("supported_services", [])

    def add_stylist(self, name: str, gender: str = None, specialties: str = None) -> Optional[int]:
        try:
            return self.stylist_repo.add_stylist(name, gender, specialties)
        except Exception as exc:
            logger.error("添加发型师失败: %s", exc)
            return None

    def get_all_specialties(self) -> List[str]:
        try:
            return self.stylist_repo.get_all_specialties()
        except Exception as exc:
            logger.error("获取发型师专长列表失败: %s", exc)
            return []

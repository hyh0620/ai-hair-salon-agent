"""Appointment service with deterministic schedule and catalog rules."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence
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


@dataclass(frozen=True)
class AppointmentLifecycleResult:
    """Stable service result shared by REST and chat lifecycle flows."""

    success: bool
    status: str
    appointment: Optional[Dict[str, Any]] = None
    appointments: tuple[Dict[str, Any], ...] = ()
    current_version: Optional[int] = None
    reason: Optional[str] = None
    internal_reason: Optional[str] = None
    transaction_id: Optional[str] = None


class _BookingRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _LifecycleRejected(Exception):
    def __init__(
        self,
        status: str,
        *,
        reason: Optional[str] = None,
        current_version: Optional[int] = None,
        internal_reason: Optional[str] = None,
    ):
        super().__init__(reason or status)
        self.status = status
        self.reason = reason or status
        self.current_version = current_version
        self.internal_reason = internal_reason


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
        *,
        owner_id: Optional[str] = None,
    ) -> bool:
        """Compatibility wrapper that preserves the historical bool contract."""
        return self.save_appointment_detailed(
            stylist_id,
            start_time,
            end_time,
            appointment_history,
            session_id,
            owner_id=owner_id,
        ).success

    def save_appointment_detailed(
        self,
        stylist_id: str,
        start_time: datetime,
        end_time: datetime,
        appointment_history: Dict[str, Any],
        session_id: str,
        *,
        owner_id: Optional[str] = None,
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
            resolved_owner_id = str(
                owner_id or details.get("user_id") or session_id
            )
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
                    user_id=resolved_owner_id,
                    session_id=session_id,
                    stylist_id=stylist_id_int,
                    service_key=service.key,
                    service_name=service.name,
                    start_time=start_time,
                    end_time=end_time,
                    duration_minutes=service.standard_duration,
                    price=service.standard_price,
                    notes=details.get("notes"),
                    updated_at=self._database_now(),
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

    def list_user_appointments(
        self,
        owner_id: str,
        *,
        future_only: bool = True,
        target_date: Optional[date] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        statuses: Sequence[str] = ("confirmed",),
        trace_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        """List owner-scoped appointments from the current database state."""
        if not str(owner_id or "").strip() or (date_from and date_to and date_from > date_to):
            return AppointmentLifecycleResult(
                False,
                "validation_error",
                reason="invalid_query",
            )
        try:
            with self.db_router.session_manager.session_scope() as session:
                rows = self.appointment_repo.list_owned_appointments_in_session(
                    session,
                    owner_id=str(owner_id),
                    statuses=statuses,
                    starts_after=self._database_now() if future_only else None,
                    target_date=target_date,
                    date_from=date_from,
                    date_to=date_to,
                )
                appointments = tuple(
                    self._appointment_to_dict(appointment, stylist_name)
                    for appointment, stylist_name in rows
                )
            self._log_lifecycle(
                operation="appointment_list",
                actor_id=owner_id,
                trace_id=trace_id,
                transaction_status="read",
                reason="success",
                candidate_count=len(appointments),
            )
            return AppointmentLifecycleResult(
                True,
                "success",
                appointments=appointments,
            )
        except Exception as exc:
            logger.exception(
                "appointment_lifecycle operation=appointment_list trace_id=%s actor_id=%s "
                "transaction_status=rollback reason=persistence_error error_type=%s",
                trace_id,
                self._actor_log_value(owner_id),
                type(exc).__name__,
            )
            return AppointmentLifecycleResult(
                False,
                "persistence_error",
                reason="persistence_error",
            )

    def get_user_appointment(
        self,
        appointment_id: int,
        owner_id: str,
        *,
        trace_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        """Read one appointment without exposing whether another owner has that ID."""
        try:
            with self.db_router.session_manager.session_scope() as session:
                appointment, stylist_name = self._get_owned_appointment_or_raise(
                    session,
                    appointment_id=appointment_id,
                    owner_id=owner_id,
                )
                snapshot = self._appointment_to_dict(appointment, stylist_name)
            self._log_lifecycle(
                operation="appointment_get",
                actor_id=owner_id,
                appointment_id=appointment_id,
                trace_id=trace_id,
                current_version=snapshot["version"],
                transaction_status="read",
                reason="success",
            )
            return AppointmentLifecycleResult(
                True,
                "success",
                appointment=snapshot,
                current_version=snapshot["version"],
            )
        except _LifecycleRejected as exc:
            self._log_lifecycle(
                operation="appointment_get",
                actor_id=owner_id,
                appointment_id=appointment_id,
                trace_id=trace_id,
                current_version=exc.current_version,
                transaction_status="not_found",
                reason=exc.internal_reason or exc.reason,
            )
            return self._lifecycle_failure(exc)
        except Exception as exc:
            logger.exception(
                "appointment_lifecycle operation=appointment_get trace_id=%s actor_id=%s "
                "appointment_id=%s transaction_status=rollback reason=persistence_error "
                "error_type=%s",
                trace_id,
                self._actor_log_value(owner_id),
                appointment_id,
                type(exc).__name__,
            )
            return AppointmentLifecycleResult(
                False,
                "persistence_error",
                reason="persistence_error",
            )

    def cancel_appointment(
        self,
        appointment_id: int,
        owner_id: str,
        expected_version: int,
        *,
        trace_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        """Atomically cancel an appointment and release its persisted schedule."""
        transaction_id = uuid4().hex
        current_version = None
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                appointment, stylist_name = self._get_owned_appointment_or_raise(
                    session,
                    appointment_id=appointment_id,
                    owner_id=owner_id,
                )
                current_version = int(appointment.version or 1)
                if appointment.status == "cancelled":
                    snapshot = self._appointment_to_dict(appointment, stylist_name)
                    self._log_lifecycle(
                        operation="appointment_cancel",
                        actor_id=owner_id,
                        appointment_id=appointment_id,
                        expected_version=expected_version,
                        current_version=current_version,
                        trace_id=trace_id,
                        transaction_id=transaction_id,
                        transaction_status="no_change",
                        reason="already_cancelled",
                    )
                    return AppointmentLifecycleResult(
                        True,
                        "already_cancelled",
                        appointment=snapshot,
                        current_version=current_version,
                        reason="already_cancelled",
                        transaction_id=transaction_id,
                    )
                self._validate_modifiable_appointment(appointment)
                if current_version != expected_version:
                    raise _LifecycleRejected(
                        "stale_state",
                        current_version=current_version,
                    )

                schedule = self._get_active_schedule_or_raise(session, appointment)
                appointment.status = "cancelled"
                appointment.version = current_version + 1
                appointment.updated_at = self._database_now()
                self.stylist_repo.cancel_schedule_in_session(
                    session,
                    schedule=schedule,
                )
                session.flush()
                snapshot = self._appointment_to_dict(appointment, stylist_name)

            self._log_lifecycle(
                operation="appointment_cancel",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=snapshot["version"],
                old_stylist_id=snapshot["stylist_id"],
                new_stylist_id=snapshot["stylist_id"],
                old_start_time=snapshot["start_time"],
                new_start_time=snapshot["start_time"],
                service_key=snapshot["service_key"],
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="commit",
                reason="success",
            )
            return AppointmentLifecycleResult(
                True,
                "success",
                appointment=snapshot,
                current_version=snapshot["version"],
                transaction_id=transaction_id,
            )
        except _LifecycleRejected as exc:
            self._log_lifecycle(
                operation="appointment_cancel",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=exc.current_version or current_version,
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="rollback",
                reason=exc.internal_reason or exc.reason,
            )
            return self._lifecycle_failure(exc, transaction_id)
        except IntegrityError as exc:
            reason = self._integrity_reason(exc)
            self._log_lifecycle(
                operation="appointment_cancel",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=current_version,
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="rollback",
                reason=reason,
            )
            return AppointmentLifecycleResult(
                False,
                reason,
                reason=reason,
                current_version=current_version,
                transaction_id=transaction_id,
            )
        except Exception as exc:
            logger.exception(
                "appointment_lifecycle operation=appointment_cancel trace_id=%s actor_id=%s "
                "appointment_id=%s expected_version=%s current_version=%s transaction_id=%s "
                "transaction_status=rollback reason=persistence_error error_type=%s",
                trace_id,
                self._actor_log_value(owner_id),
                appointment_id,
                expected_version,
                current_version,
                transaction_id,
                type(exc).__name__,
            )
            return AppointmentLifecycleResult(
                False,
                "persistence_error",
                reason="persistence_error",
                current_version=current_version,
                transaction_id=transaction_id,
            )

    def preview_appointment_update(
        self,
        appointment_id: int,
        owner_id: str,
        expected_version: int,
        *,
        target_date: Optional[date] = None,
        target_time: Optional[time] = None,
        stylist_id: Optional[int] = None,
        stylist_name: Optional[str] = None,
        service_value: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        """Validate and preview an update without writing or incrementing version."""
        try:
            with self.db_router.session_manager.session_scope() as session:
                plan = self._prepare_update_in_session(
                    session,
                    appointment_id=appointment_id,
                    owner_id=owner_id,
                    expected_version=expected_version,
                    target_date=target_date,
                    target_time=target_time,
                    stylist_id=stylist_id,
                    stylist_name=stylist_name,
                    service_value=service_value,
                )
            status = "no_change" if not plan["changed"] else "confirmation_required"
            return AppointmentLifecycleResult(
                True,
                status,
                appointment=plan["snapshot"],
                current_version=plan["current_version"],
                reason=status,
            )
        except _LifecycleRejected as exc:
            return self._lifecycle_failure(exc)
        except Exception as exc:
            logger.exception(
                "appointment_lifecycle operation=appointment_update_preview trace_id=%s "
                "actor_id=%s appointment_id=%s transaction_status=rollback "
                "reason=persistence_error error_type=%s",
                trace_id,
                self._actor_log_value(owner_id),
                appointment_id,
                type(exc).__name__,
            )
            return AppointmentLifecycleResult(
                False,
                "persistence_error",
                reason="persistence_error",
            )

    def update_appointment(
        self,
        appointment_id: int,
        owner_id: str,
        expected_version: int,
        *,
        target_date: Optional[date] = None,
        target_time: Optional[time] = None,
        stylist_id: Optional[int] = None,
        stylist_name: Optional[str] = None,
        service_value: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        """Atomically update appointment facts and its one active schedule."""
        transaction_id = uuid4().hex
        current_version = None
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                plan = self._prepare_update_in_session(
                    session,
                    appointment_id=appointment_id,
                    owner_id=owner_id,
                    expected_version=expected_version,
                    target_date=target_date,
                    target_time=target_time,
                    stylist_id=stylist_id,
                    stylist_name=stylist_name,
                    service_value=service_value,
                )
                current_version = plan["current_version"]
                if not plan["changed"]:
                    self._log_lifecycle(
                        operation="appointment_update",
                        actor_id=owner_id,
                        appointment_id=appointment_id,
                        expected_version=expected_version,
                        current_version=current_version,
                        trace_id=trace_id,
                        transaction_id=transaction_id,
                        transaction_status="no_change",
                        reason="no_change",
                    )
                    return AppointmentLifecycleResult(
                        True,
                        "no_change",
                        appointment=plan["snapshot"],
                        current_version=current_version,
                        reason="no_change",
                        transaction_id=transaction_id,
                    )

                appointment = plan["appointment"]
                schedule = plan["schedule"]
                service = plan["service"]
                appointment.stylist_id = plan["stylist"]["id"]
                appointment.service_key = service.key
                appointment.service_name = service.name
                appointment.start_time = plan["start_time"]
                appointment.end_time = plan["end_time"]
                appointment.duration_minutes = service.standard_duration
                appointment.price = service.standard_price
                appointment.updated_at = self._database_now()
                appointment.version = current_version + 1
                self.stylist_repo.update_schedule_in_session(
                    session,
                    schedule=schedule,
                    stylist_id=plan["stylist"]["id"],
                    start_time=plan["start_time"],
                    end_time=plan["end_time"],
                )
                session.flush()
                snapshot = self._appointment_to_dict(
                    appointment,
                    plan["stylist"]["name"],
                )

            self._log_lifecycle(
                operation="appointment_update",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=snapshot["version"],
                old_stylist_id=plan["old_stylist_id"],
                new_stylist_id=snapshot["stylist_id"],
                old_start_time=plan["old_start_time"],
                new_start_time=snapshot["start_time"],
                service_key=snapshot["service_key"],
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="commit",
                reason="success",
            )
            return AppointmentLifecycleResult(
                True,
                "success",
                appointment=snapshot,
                current_version=snapshot["version"],
                transaction_id=transaction_id,
            )
        except _LifecycleRejected as exc:
            self._log_lifecycle(
                operation="appointment_update",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=exc.current_version or current_version,
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="rollback",
                reason=exc.internal_reason or exc.reason,
            )
            return self._lifecycle_failure(exc, transaction_id)
        except IntegrityError as exc:
            reason = self._integrity_reason(exc)
            self._log_lifecycle(
                operation="appointment_update",
                actor_id=owner_id,
                appointment_id=appointment_id,
                expected_version=expected_version,
                current_version=current_version,
                trace_id=trace_id,
                transaction_id=transaction_id,
                transaction_status="rollback",
                reason=reason,
            )
            return AppointmentLifecycleResult(
                False,
                reason,
                reason=reason,
                current_version=current_version,
                transaction_id=transaction_id,
            )
        except Exception as exc:
            logger.exception(
                "appointment_lifecycle operation=appointment_update trace_id=%s actor_id=%s "
                "appointment_id=%s expected_version=%s current_version=%s transaction_id=%s "
                "transaction_status=rollback reason=persistence_error error_type=%s",
                trace_id,
                self._actor_log_value(owner_id),
                appointment_id,
                expected_version,
                current_version,
                transaction_id,
                type(exc).__name__,
            )
            return AppointmentLifecycleResult(
                False,
                "persistence_error",
                reason="persistence_error",
                current_version=current_version,
                transaction_id=transaction_id,
            )

    def _prepare_update_in_session(
        self,
        session,
        *,
        appointment_id: int,
        owner_id: str,
        expected_version: int,
        target_date: Optional[date],
        target_time: Optional[time],
        stylist_id: Optional[int],
        stylist_name: Optional[str],
        service_value: Optional[str],
    ) -> Dict[str, Any]:
        if stylist_id is not None and stylist_name:
            raise _LifecycleRejected("validation_error", reason="ambiguous_stylist")

        appointment, old_stylist_name = self._get_owned_appointment_or_raise(
            session,
            appointment_id=appointment_id,
            owner_id=owner_id,
        )
        current_version = int(appointment.version or 1)
        self._validate_modifiable_appointment(appointment)
        if current_version != expected_version:
            raise _LifecycleRejected(
                "stale_state",
                current_version=current_version,
            )
        schedule = self._get_active_schedule_or_raise(session, appointment)

        service = normalize_service(
            service_value if service_value is not None else appointment.service_key
        )
        if not service:
            raise _LifecycleRejected(
                "validation_error",
                reason="unknown_service",
                current_version=current_version,
            )

        if stylist_id is not None:
            stylist = self.stylist_repo.get_stylist_by_id_in_session(session, int(stylist_id))
        elif stylist_name:
            stylist = self.stylist_repo.get_stylist_by_name_in_session(session, stylist_name)
        else:
            stylist = self.stylist_repo.get_stylist_by_id_in_session(
                session,
                int(appointment.stylist_id),
            )
        if not stylist:
            raise _LifecycleRejected(
                "not_found",
                reason="stylist_not_found",
                current_version=current_version,
            )
        if not self.stylist_supports_service(stylist, service.key):
            raise _LifecycleRejected(
                "service_not_supported",
                reason="stylist_service_unsupported",
                current_version=current_version,
            )

        old_start = self._as_database_datetime(appointment.start_time)
        new_date = target_date or old_start.date()
        new_time = target_time or old_start.time().replace(second=0, microsecond=0)
        new_start = datetime.combine(
            new_date,
            new_time.replace(tzinfo=None),
        )
        new_end = new_start + timedelta(minutes=service.standard_duration)
        changed = any((
            int(appointment.stylist_id) != int(stylist["id"]),
            appointment.service_key != service.key,
            old_start != new_start,
            self._as_database_datetime(appointment.end_time) != new_end,
            int(appointment.duration_minutes) != service.standard_duration,
            int(appointment.price) != service.standard_price,
        ))

        proposed = self._appointment_to_dict(appointment, stylist["name"])
        proposed.update({
            "stylist_id": int(stylist["id"]),
            "stylist_name": stylist["name"],
            "service_key": service.key,
            "service_name": service.name,
            "start_time": new_start,
            "end_time": new_end,
            "duration_minutes": service.standard_duration,
            "price": service.standard_price,
        })
        if not changed:
            return {
                "appointment": appointment,
                "schedule": schedule,
                "stylist": stylist,
                "service": service,
                "start_time": new_start,
                "end_time": new_end,
                "snapshot": proposed,
                "changed": False,
                "current_version": current_version,
                "old_stylist_id": int(appointment.stylist_id),
                "old_start_time": old_start,
            }

        if not self.is_future_start_time(new_start):
            raise _LifecycleRejected(
                "invalid_time",
                reason="past_appointment",
                current_version=current_version,
            )
        if not self.is_within_business_hours(new_start, new_end):
            raise _LifecycleRejected(
                "outside_business_hours",
                current_version=current_version,
            )
        if self.stylist_repo.has_schedule_conflict_in_session(
            session,
            stylist_id=int(stylist["id"]),
            start_time=new_start,
            end_time=new_end,
            exclude_appointment_id=int(appointment.id),
            exclude_schedule_id=int(schedule.id),
        ):
            raise _LifecycleRejected(
                "conflict",
                reason="schedule_conflict",
                current_version=current_version,
            )
        return {
            "appointment": appointment,
            "schedule": schedule,
            "stylist": stylist,
            "service": service,
            "start_time": new_start,
            "end_time": new_end,
            "snapshot": proposed,
            "changed": True,
            "current_version": current_version,
            "old_stylist_id": int(appointment.stylist_id),
            "old_stylist_name": old_stylist_name,
            "old_start_time": old_start,
        }

    def _get_owned_appointment_or_raise(
        self,
        session,
        *,
        appointment_id: int,
        owner_id: str,
    ):
        row = self.appointment_repo.get_owned_appointment_in_session(
            session,
            appointment_id=int(appointment_id),
            owner_id=str(owner_id),
        )
        if row:
            return row
        actual_owner = self.appointment_repo.appointment_owner_in_session(
            session,
            appointment_id=int(appointment_id),
        )
        raise _LifecycleRejected(
            "not_found",
            internal_reason=(
                "ownership_mismatch" if actual_owner is not None else "appointment_not_found"
            ),
        )

    def _get_active_schedule_or_raise(self, session, appointment):
        schedules = self.stylist_repo.get_schedules_for_appointment_in_session(
            session,
            appointment_id=int(appointment.id),
        )
        active = [schedule for schedule in schedules if schedule.status == "busy"]
        if len(active) != 1:
            raise _LifecycleRejected(
                "persistence_error",
                reason="schedule_invariant_violation",
                current_version=int(appointment.version or 1),
            )
        return active[0]

    def _validate_modifiable_appointment(self, appointment) -> None:
        current_version = int(appointment.version or 1)
        if appointment.status != "confirmed":
            raise _LifecycleRejected(
                "not_modifiable",
                reason=f"status_{appointment.status}",
                current_version=current_version,
            )
        if not self.is_future_start_time(self._as_database_datetime(appointment.start_time)):
            raise _LifecycleRejected(
                "not_modifiable",
                reason="past_appointment",
                current_version=current_version,
            )

    @staticmethod
    def _appointment_to_dict(appointment, stylist_name: str) -> Dict[str, Any]:
        return {
            "appointment_id": int(appointment.id),
            "owner_id": str(appointment.user_id),
            "stylist_id": int(appointment.stylist_id),
            "stylist_name": stylist_name,
            "service_key": appointment.service_key,
            "service_name": appointment.service_name,
            "price": int(appointment.price),
            "duration_minutes": int(appointment.duration_minutes),
            "start_time": appointment.start_time,
            "end_time": appointment.end_time,
            "status": appointment.status,
            "version": int(appointment.version or 1),
            "created_at": appointment.created_at,
            "updated_at": appointment.updated_at,
        }

    @staticmethod
    def _as_database_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(time_config.BEIJING_TZ).replace(tzinfo=None)

    @staticmethod
    def _database_now() -> datetime:
        return time_config.now().astimezone(time_config.BEIJING_TZ).replace(tzinfo=None)

    @staticmethod
    def _actor_log_value(actor_id: Any) -> str:
        value = str(actor_id or "unknown").encode("utf-8")
        return f"actor-{hashlib.sha256(value).hexdigest()[:12]}"

    @staticmethod
    def _integrity_reason(exc: IntegrityError) -> str:
        return "conflict" if "schedule_conflict" in str(exc.orig) else "persistence_error"

    @staticmethod
    def _lifecycle_failure(
        exc: _LifecycleRejected,
        transaction_id: Optional[str] = None,
    ) -> AppointmentLifecycleResult:
        return AppointmentLifecycleResult(
            False,
            exc.status,
            current_version=exc.current_version,
            reason=exc.reason,
            internal_reason=exc.internal_reason,
            transaction_id=transaction_id,
        )

    @classmethod
    def _log_lifecycle(
        cls,
        *,
        operation: str,
        actor_id: Any,
        appointment_id: Optional[int] = None,
        expected_version: Optional[int] = None,
        current_version: Optional[int] = None,
        old_stylist_id: Optional[int] = None,
        new_stylist_id: Optional[int] = None,
        old_start_time: Optional[datetime] = None,
        new_start_time: Optional[datetime] = None,
        service_key: Optional[str] = None,
        trace_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        transaction_status: str,
        reason: str,
        candidate_count: Optional[int] = None,
    ) -> None:
        logger.info(
            "appointment_lifecycle operation=%s trace_id=%s actor_id=%s appointment_id=%s "
            "expected_version=%s current_version=%s transaction_id=%s old_stylist_id=%s "
            "new_stylist_id=%s old_start_time=%s new_start_time=%s service_key=%s "
            "candidate_count=%s transaction_status=%s reason=%s",
            operation,
            trace_id,
            cls._actor_log_value(actor_id),
            appointment_id,
            expected_version,
            current_version,
            transaction_id,
            old_stylist_id,
            new_stylist_id,
            old_start_time,
            new_start_time,
            service_key,
            candidate_count,
            transaction_status,
            reason,
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

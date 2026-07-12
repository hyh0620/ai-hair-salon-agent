"""Appointment service with deterministic schedule and catalog rules."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.time_config import time_config
from db.db_router import DatabaseRouter
from services.service_catalog import normalize_service, parse_duration_minutes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppointmentSaveResult:
    success: bool
    appointment_id: Optional[int] = None
    schedule_id: Optional[int] = None
    reason: Optional[str] = None


class AppointmentService:
    """Business logic for appointment persistence and stylist availability."""

    def __init__(self, db_path: str = None):
        self.db_router = DatabaseRouter(db_path)
        self.stylist_repo = self.db_router.stylists

    def build_appointment_details(self, appointment_history: Dict[str, Any]) -> Dict[str, Any]:
        service = normalize_service(appointment_history.get("project"))
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
        try:
            if not self.is_within_business_hours(start_time, end_time):
                logger.warning("预约时间不在营业时间内: %s 到 %s", start_time, end_time)
                return AppointmentSaveResult(False, reason="outside_business_hours")

            stylist_id_int = int(stylist_id)
            if not self.is_stylist_available(stylist_id_int, start_time, end_time):
                logger.warning("发型师 %s 在 %s 到 %s 已有预约冲突", stylist_id, start_time, end_time)
                return AppointmentSaveResult(False, reason="schedule_conflict")

            appointment_id = int(time.time() * 1000)
            schedule_id = self.stylist_repo.add_schedule(
                stylist_id=stylist_id_int,
                start_time=start_time,
                end_time=end_time,
                status="busy",
                appointment_id=appointment_id,
            )
            logger.info(
                "预约已保存: 发型师ID=%s, 时间=%s 到 %s, 预约ID=%s",
                stylist_id,
                start_time,
                end_time,
                appointment_id,
            )
            return AppointmentSaveResult(
                success=True,
                appointment_id=appointment_id,
                schedule_id=schedule_id,
            )
        except Exception as exc:
            logger.error("保存预约失败: %s", exc)
            return AppointmentSaveResult(False, reason="persistence_error")

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

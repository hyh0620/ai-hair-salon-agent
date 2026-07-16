"""Deterministic parsing for appointment lifecycle conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from config.time_config import time_config
from services.service_catalog import normalize_service

from .availability_parser import parse_booking_temporal_slots


LIST_APPOINTMENTS = "list_appointments"
GET_APPOINTMENT = "get_appointment"
CANCEL_APPOINTMENT = "cancel_appointment"
UPDATE_APPOINTMENT = "update_appointment"
RESCHEDULE_APPOINTMENT = "reschedule_appointment"

LIFECYCLE_INTENTS = {
    LIST_APPOINTMENTS,
    GET_APPOINTMENT,
    CANCEL_APPOINTMENT,
    UPDATE_APPOINTMENT,
    RESCHEDULE_APPOINTMENT,
}


@dataclass(frozen=True)
class ParsedLifecycleRequest:
    intent: Optional[str] = None
    appointment_id: Optional[int] = None
    target_date: Optional[date] = None
    target_time: Optional[time] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    period_label: Optional[str] = None
    stylist_name: Optional[str] = None
    service_value: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return any((
            self.target_date,
            self.target_time,
            self.stylist_name,
            self.service_value,
        ))


def detect_lifecycle_intent(text: str) -> Optional[str]:
    normalized = _compact(text)
    if not normalized:
        return None

    if re.search(r"(取消|撤销).{0,12}预约|预约.{0,12}(取消|撤销)", normalized):
        return CANCEL_APPOINTMENT
    if "改期" in normalized or re.search(
        r"(把|将)?预约.{0,18}(改到|换到|挪到|延到)|"
        r"(改到|换到|挪到|延到).{0,18}(预约|时间|日期)",
        normalized,
    ):
        return RESCHEDULE_APPOINTMENT
    if re.search(
        r"(修改|更改|调整).{0,10}预约|我想改一下预约|"
        r"我想换一个(?:发型师|理发师|老师)|"
        r"预约.{0,18}(换发型师|换老师|改服务|换服务|改成|换成)|"
        r"把预约.{0,18}(改成|换成)",
        normalized,
    ):
        return UPDATE_APPOINTMENT
    if extract_appointment_id(normalized) is not None and re.search(
        r"(查询|查看|看看|详情|预约)", normalized
    ):
        return GET_APPOINTMENT
    if re.search(
        r"查看我的预约|查询我的预约|我的预约(?:记录|信息|情况)?|"
        r"我预约了什么|我订了什么|有没有预约|预约记录|"
        r"帮我看看.{0,10}预约|我.{0,10}预约是什么时间",
        normalized,
    ):
        return LIST_APPOINTMENTS
    return None


def parse_lifecycle_request(
    text: str,
    *,
    now: Optional[datetime] = None,
    stylist_names: Iterable[str] = (),
) -> ParsedLifecycleRequest:
    now = now or time_config.now()
    normalized = _compact(text)
    temporal = parse_booking_temporal_slots(text, now=now)
    date_from, date_to = _date_range(normalized, now)
    service_value = _target_service(normalized)
    return ParsedLifecycleRequest(
        intent=detect_lifecycle_intent(text),
        appointment_id=extract_appointment_id(normalized),
        target_date=temporal.target_date,
        target_time=temporal.exact_time,
        date_from=date_from,
        date_to=date_to,
        period_label=temporal.period_label if temporal.exact_time is None else None,
        stylist_name=next((name for name in stylist_names if name and name in text), None),
        service_value=service_value,
    )


def extract_appointment_id(text: str) -> Optional[int]:
    normalized = _compact(text)
    patterns = (
        r"预约(?:编号|id|ID)?[：:#]?([1-9]\d*)",
        r"appointment(?:id)?[：:#]?([1-9]\d*)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _date_range(text: str, now: datetime) -> tuple[Optional[date], Optional[date]]:
    today = now.date()
    if "下周" in text and not re.search(r"下周(?:周|星期)[一二三四五六日天]", text):
        days_to_next_monday = 7 - today.weekday()
        start = today + timedelta(days=days_to_next_monday)
        return start, start + timedelta(days=6)
    if ("本周" in text or "这周" in text) and not re.search(
        r"(?:本周|这周)(?:周|星期)[一二三四五六日天]", text
    ):
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    return None, None


def _target_service(text: str) -> Optional[str]:
    target = text
    match = re.search(r"(?:改成|换成|改为|换为)(.+)$", text)
    if match:
        target = match.group(1)
    service = normalize_service(target)
    return service.key if service else None


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())

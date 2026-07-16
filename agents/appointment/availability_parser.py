"""High-precision parsing for availability-search conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from config.time_config import time_config
from services.service_catalog import normalize_service, normalize_specialty, service_for_specialty


CREATE_BOOKING = "create_booking"
SEARCH_AVAILABILITY = "search_availability"
CONSULTATION = "consultation"
AMBIGUOUS = "ambiguous"

_CHINESE_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass(frozen=True)
class ParsedAvailabilityRequest:
    intent: str
    target_date: Optional[date] = None
    range_start: Optional[time] = None
    range_end: Optional[time] = None
    exact_time: Optional[time] = None
    period_label: Optional[str] = None
    service_key: Optional[str] = None
    service_name: Optional[str] = None
    specialty: Optional[str] = None
    stylist_name: Optional[str] = None
    date_label: Optional[str] = None


@dataclass(frozen=True)
class BookingTemporalSlots:
    target_date: Optional[date] = None
    exact_time: Optional[time] = None
    range_start: Optional[time] = None
    range_end: Optional[time] = None
    period_label: Optional[str] = None
    date_label: Optional[str] = None

    @property
    def has_exact_datetime(self) -> bool:
        return self.target_date is not None and self.exact_time is not None

    @property
    def has_search_range(self) -> bool:
        return (
            self.target_date is not None
            and self.exact_time is None
            and self.range_start is not None
            and self.range_end is not None
        )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def detect_message_intent(text: str) -> str:
    """Classify only business routing intent; it never queries data or mutates state."""
    # Imported lazily because lifecycle parsing reuses the temporal helpers in this module.
    from .lifecycle_parser import detect_lifecycle_intent

    lifecycle_intent = detect_lifecycle_intent(text)
    if lifecycle_intent:
        return lifecycle_intent
    normalized = _compact(text)
    booking_markers = ("预约", "预订", "我想约", "想约", "我要约", "帮我约", "约一下", "安排")
    if any(marker in normalized for marker in booking_markers):
        return CREATE_BOOKING

    consultation_markers = (
        "适合什么", "适合哪", "怎么护理", "如何护理", "注意事项", "能保持多久",
        "可以保持多久", "是什么效果", "什么效果", "几点营业", "营业时间", "价格多少",
        "怎么打理", "如何打理", "主要负责什么", "怎么成为", "门店有哪些理发师",
        "门店有哪些发型师", "门店有哪些老师",
    )
    if any(marker in normalized for marker in consultation_markers):
        return CONSULTATION

    temporal_query = bool(re.search(
        r"(今天|明天|后天|(?:下周|本周|这周)?(?:周|星期)[一二三四五六日天]|"
        r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}日|"
        r"上午|中午|下午|晚上|\d{1,2}:\d{2}|[零一二两三四五六七八九十\d]+点)",
        normalized,
    ))
    person_query = bool(re.search(
        r"谁|(哪些|哪位|哪个).{0,8}(老师|发型师|理发师)|"
        r"(老师|发型师|理发师).{0,8}(谁|哪些|哪位|哪个)",
        normalized,
    ))
    availability_status = bool(re.search(
        r"(有空|空闲|有时间|可约|可以预约|可以安排|档期)",
        normalized,
    ))
    capability_query = bool(re.search(
        r"(谁|哪些|哪位|哪个).{0,8}(能|可以|会).{0,12}(做|剪|染|烫)",
        normalized,
    ))
    search_action = bool(re.search(
        r"(找|查|看看|查询|安排).{0,18}(老师|发型师|理发师)|"
        r"(擅长|会做).{0,12}(老师|发型师|理发师)",
        normalized,
    ))
    if (
        (availability_status and (person_query or temporal_query))
        or (temporal_query and person_query and capability_query)
        or search_action
    ):
        return SEARCH_AVAILABILITY
    if normalized in {"确认", "好的", "好", "可以", "取消", "不用了", "不确认", "换一个"}:
        return CONSULTATION
    return AMBIGUOUS


def parse_availability_request(
    text: str,
    now: Optional[datetime] = None,
    stylist_names: Iterable[str] = (),
) -> ParsedAvailabilityRequest:
    now = now or time_config.now()
    normalized = _compact(text)
    specialty = _extract_specialty(normalized)
    service = normalize_service(normalized) or service_for_specialty(specialty)
    if (
        specialty
        and service
        and specialty == service.name
        and not re.search(r"(擅长|专长|会做|偏好)", normalized)
    ):
        specialty = None
    exact_time, period_start, period_end, period_label = _extract_time(normalized)
    target_date = _extract_date(normalized, now)
    return ParsedAvailabilityRequest(
        intent=detect_message_intent(normalized),
        target_date=target_date,
        range_start=period_start,
        range_end=period_end,
        exact_time=exact_time,
        period_label=period_label,
        service_key=service.key if service else None,
        service_name=service.name if service else None,
        specialty=specialty,
        stylist_name=next((name for name in stylist_names if name and name in text), None),
        date_label=_date_label(normalized, target_date),
    )


def parse_booking_temporal_slots(
    text: str,
    now: Optional[datetime] = None,
) -> BookingTemporalSlots:
    """Parse only temporal facts explicitly present in the current user turn."""
    now = now or time_config.now()
    normalized = _compact(text)
    target_date = _extract_date(normalized, now)
    exact_time, range_start, range_end, period_label = _extract_time(normalized)
    return BookingTemporalSlots(
        target_date=target_date,
        exact_time=exact_time,
        range_start=range_start,
        range_end=range_end,
        period_label=period_label,
        date_label=_date_label(normalized, target_date),
    )


def parse_selection_time(text: str) -> Optional[time]:
    exact, _, _, _ = _extract_time(_compact(text))
    return exact


def _extract_specialty(text: str) -> Optional[str]:
    return normalize_specialty(text)


def _extract_date(text: str, now: datetime) -> Optional[date]:
    today = now.date()
    if "后天" in text:
        return today + timedelta(days=2)
    if "明天" in text:
        return today + timedelta(days=1)
    if "今天" in text:
        return today

    iso_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if iso_match:
        try:
            return date(*(int(part) for part in iso_match.groups()))
        except ValueError:
            return None

    month_day_match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
    if month_day_match:
        month, day = (int(part) for part in month_day_match.groups())
        try:
            candidate = date(today.year, month, day)
            if candidate < today:
                candidate = date(today.year + 1, month, day)
            return candidate
        except ValueError:
            return None

    weekday_match = re.search(r"(下周|本周|这周)?(?:周|星期)([一二三四五六日天])", text)
    if weekday_match:
        modifier, weekday_text = weekday_match.groups()
        target_weekday = _WEEKDAYS[weekday_text]
        days_ahead = (target_weekday - today.weekday()) % 7
        if modifier == "下周":
            days_ahead = days_ahead + 7 if days_ahead else 7
        elif days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)
    return None


def _extract_time(text: str) -> tuple[Optional[time], Optional[time], Optional[time], Optional[str]]:
    start_hour, business_end = time_config.get_business_hours()
    period_ranges = {
        "上午": (time(start_hour, 0), time(12, 0)),
        "中午": (time(11, 30), time(13, 30)),
        "下午": (time(12, 0), time(18, 0)),
        "晚上": (time(18, 0), time(business_end, 0)),
    }
    period_label = next((label for label in period_ranges if label in text), None)

    if "午夜" in text:
        exact = time(0, 0)
        return exact, exact, exact, period_label

    colon_match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", text)
    if colon_match:
        hour, minute = map(int, colon_match.groups())
        exact = time(hour, minute)
        return exact, exact, exact, period_label

    clock_match = re.search(r"([零一二两三四五六七八九十\d]{1,3})点(半|([0-5]?\d)分?)?", text)
    if clock_match:
        hour = _parse_hour(clock_match.group(1))
        if hour is not None:
            minute = 30 if clock_match.group(2) == "半" else int(clock_match.group(3) or 0)
            if period_label in {"下午", "晚上"} and hour < 12:
                hour += 12
            if period_label == "中午" and hour < 10:
                hour += 12
            if 0 <= hour <= 23:
                exact = time(hour, minute)
                return exact, exact, exact, period_label

    if period_label:
        period_start, period_end = period_ranges[period_label]
        return None, period_start, period_end, period_label
    return None, None, None, None


def _date_label(text: str, target_date: Optional[date]) -> Optional[str]:
    if not target_date:
        return None
    for label in ("今天", "明天", "后天"):
        if label in text:
            return label
    weekday_match = re.search(r"(下周|本周|这周)?(?:周|星期)([一二三四五六日天])", text)
    if weekday_match:
        modifier, weekday = weekday_match.groups()
        return f"{modifier or ''}周{weekday}"
    return target_date.isoformat()


def _parse_hour(value: str) -> Optional[int]:
    if value.isdigit():
        return int(value)
    if value in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[value]
    if value.startswith("十"):
        return 10 + _CHINESE_DIGITS.get(value[1:], 0)
    if value.endswith("十"):
        return _CHINESE_DIGITS.get(value[:-1], 0) * 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        return _CHINESE_DIGITS.get(tens, 1) * 10 + _CHINESE_DIGITS.get(ones, 0)
    return None

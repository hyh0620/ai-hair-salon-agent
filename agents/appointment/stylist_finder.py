"""Find an available stylist using deterministic service and schedule rules."""

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.service_catalog import parse_duration_minutes, specialties_for


class StylistFinder:
    """Select stylists by service fit, optional preferences, and availability."""

    def __init__(self, appointment_service: Optional[AppointmentService] = None):
        self.appointment_service = appointment_service or AppointmentService()

    def parse_time_and_duration(self, start_time_str: str, duration_str: str) -> tuple:
        if not start_time_str or start_time_str == "未知":
            return None, None, None
        duration_min = parse_duration_minutes(duration_str)
        if duration_min is None:
            return None, None, None
        start_time = time_config.parse_datetime(start_time_str)
        if start_time is None:
            return None, None, None
        end_time = start_time + timedelta(minutes=duration_min)
        return start_time, end_time, duration_min

    def find_specific_stylist(
        self,
        stylist_name: str,
        start_time: datetime,
        end_time: datetime,
        yield_func: Optional[Callable] = None,
        service_value: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if yield_func:
            yield_func(f"[THOUGHT][预约机器人] 用户指定了发型师：{stylist_name}，正在查询档期...\n")

        stylist = self.appointment_service.get_stylist_by_name(stylist_name)
        if not stylist:
            if yield_func:
                yield_func(f"[THOUGHT][预约机器人] 未找到名为'{stylist_name}'的发型师\n")
            return None

        if service_value and not self.appointment_service.stylist_supports_service(
            stylist,
            service_value,
        ):
            if yield_func:
                yield_func(f"[THOUGHT][预约机器人] {stylist_name}不支持所选服务\n")
            return None

        if self.appointment_service.is_stylist_available(stylist["id"], start_time, end_time):
            if yield_func:
                yield_func(f"[THOUGHT][预约机器人] {stylist_name}在指定时间可预约\n")
            return stylist

        if yield_func:
            yield_func(f"[THOUGHT][预约机器人] {stylist_name}在指定时间已有预约\n")
        return None

    def score_stylist(self, stylist: Dict[str, Any], appointment_history: Dict[str, Any]) -> int:
        specialties = (stylist.get("specialties") or "").lower()
        terms = specialties_for(
            appointment_history.get("project"),
            [
                appointment_history.get("preference"),
                appointment_history.get("style_preference"),
            ],
        )
        score = 0
        for term in terms:
            term_text = str(term).lower()
            if term_text and term_text in specialties:
                score += 3
            elif term_text and any(part and part in specialties for part in term_text.split()):
                score += 1

        gender = appointment_history.get("gender")
        if gender and gender not in ("未知", "无") and stylist.get("gender") == gender:
            score += 1
        return score

    def rank_stylists(self, stylists: List[Dict[str, Any]], appointment_history: Dict[str, Any]) -> List[Dict[str, Any]]:
        gender = appointment_history.get("gender")
        if gender and gender not in ("未知", "无"):
            gender_matched = [item for item in stylists if item.get("gender") == gender]
            if gender_matched:
                stylists = gender_matched

        return sorted(
            stylists,
            key=lambda item: (self.score_stylist(item, appointment_history), -int(item.get("id", 0))),
            reverse=True,
        )

    def find_similar_available_stylist(
        self,
        target_stylist: Dict[str, Any],
        appointment_history: Dict[str, Any],
        start_time: datetime,
        end_time: datetime,
        yield_func: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        stylists = [
            item for item in self.appointment_service.get_all_stylists()
            if item["id"] != target_stylist["id"]
            and self.appointment_service.stylist_supports_service(
                item,
                appointment_history.get("service_key") or appointment_history.get("project"),
            )
        ]
        for stylist in self.rank_stylists(stylists, appointment_history):
            if self.appointment_service.is_stylist_available(stylist["id"], start_time, end_time):
                if yield_func:
                    yield_func(f"[THOUGHT][预约机器人] 推荐相近专长且可预约的发型师：{stylist['name']}\n")
                return stylist
        return None

    def find_available_stylist(
        self,
        appointment_history: Dict[str, Any],
        start_time: datetime,
        end_time: datetime,
        yield_func: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        service_value = appointment_history.get("service_key") or appointment_history.get("project")
        stylists = [
            item
            for item in self.appointment_service.get_all_stylists()
            if self.appointment_service.stylist_supports_service(item, service_value)
        ]
        if not stylists:
            if yield_func:
                yield_func("[THOUGHT][预约机器人] 没有找到发型师数据\n")
            return None

        for stylist in self.rank_stylists(stylists, appointment_history):
            if self.appointment_service.is_stylist_available(stylist["id"], start_time, end_time):
                if yield_func:
                    yield_func(f"[THOUGHT][预约机器人] 找到可预约发型师：{stylist['name']}\n")
                return stylist

        if yield_func:
            yield_func("[THOUGHT][预约机器人] 当前条件下没有可预约发型师\n")
        return None

    def find_stylist_with_thought(
        self,
        appointment_history: Dict[str, Any],
        yield_func: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        start_time, end_time, _ = self.parse_time_and_duration(
            appointment_history.get("start_time"),
            appointment_history.get("duration"),
        )
        if not start_time or not end_time:
            if yield_func:
                yield_func("[THOUGHT][预约机器人] 预约时间或时长信息不完整，无法检索发型师\n")
            return None

        if yield_func:
            yield_func("[THOUGHT][预约机器人] 正在检查时间、服务项目和发型师档期...\n")

        stylist_name = appointment_history.get("stylist_name")
        if stylist_name and stylist_name != "未知":
            specific = self.find_specific_stylist(
                stylist_name,
                start_time,
                end_time,
                yield_func,
                appointment_history.get("service_key") or appointment_history.get("project"),
            )
            if specific:
                return specific

            target = self.appointment_service.get_stylist_by_name(stylist_name)
            if target:
                recommended = self.find_similar_available_stylist(
                    target,
                    appointment_history,
                    start_time,
                    end_time,
                    yield_func,
                )
                if recommended:
                    return {
                        "requires_confirmation": True,
                        "original_stylist": target,
                        "recommended_stylist": recommended,
                    }
            return None

        return self.find_available_stylist(appointment_history, start_time, end_time, yield_func)

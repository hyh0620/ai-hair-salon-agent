"""Owner-scoped lifecycle operations coordinated by transient chat state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, Optional

from config.appointment_status import appointment_status_label
from config.time_config import time_config
from services.appointment_service import AppointmentLifecycleResult, AppointmentService

from .availability_parser import parse_booking_temporal_slots
from .lifecycle_parser import (
    CANCEL_APPOINTMENT,
    GET_APPOINTMENT,
    LIST_APPOINTMENTS,
    RESCHEDULE_APPOINTMENT,
    UPDATE_APPOINTMENT,
    ParsedLifecycleRequest,
    is_abort_current_operation,
    is_bare_cancel,
    parse_lifecycle_request,
)


@dataclass(frozen=True)
class LifecycleChatResult:
    message: str
    complete: bool


class AppointmentLifecycleProcessor:
    """Coordinate lifecycle dialog while keeping SQLite as the authority."""

    _STATE_KEYS = (
        "pending_lifecycle_action",
        "pending_appointment_candidates",
        "selected_appointment_id",
        "selected_appointment_version",
        "pending_changes",
        "awaiting_lifecycle_selection",
        "awaiting_lifecycle_changes",
        "awaiting_lifecycle_confirmation",
    )

    def __init__(self, appointment_service: AppointmentService):
        self.appointment_service = appointment_service

    @classmethod
    def is_active(cls, history: Dict[str, Any]) -> bool:
        return any(history.get(key) for key in cls._STATE_KEYS)

    @classmethod
    def clear_state(cls, history: Dict[str, Any]) -> None:
        for key in cls._STATE_KEYS:
            history.pop(key, None)

    def handle(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
        *,
        intent: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> LifecycleChatResult:
        now = now or time_config.now()
        if self.is_active(history):
            return self._continue_flow(user_input, history, actor_id, now)

        parsed = self._parse(user_input, now)
        action = intent or parsed.intent
        if action in {LIST_APPOINTMENTS, GET_APPOINTMENT}:
            return self._start_query(action, parsed, history, actor_id)
        if action == CANCEL_APPOINTMENT:
            return self._start_cancel(parsed, history, actor_id)
        if action in {UPDATE_APPOINTMENT, RESCHEDULE_APPOINTMENT}:
            return self._start_update(user_input, parsed, history, actor_id)
        return LifecycleChatResult("我没有识别到预约生命周期操作，请重新说明。", True)

    def _parse(self, text: str, now: datetime) -> ParsedLifecycleRequest:
        stylists = self.appointment_service.get_all_stylists()
        return parse_lifecycle_request(
            text,
            now=now,
            stylist_names=[item["name"] for item in stylists],
        )

    def _start_query(
        self,
        action: str,
        parsed: ParsedLifecycleRequest,
        history: Dict[str, Any],
        actor_id: str,
    ) -> LifecycleChatResult:
        if parsed.appointment_id is not None:
            result = self.appointment_service.get_user_appointment(
                parsed.appointment_id,
                actor_id,
            )
            if not result.success:
                return LifecycleChatResult(self._failure_message(result, "查询"), True)
            return LifecycleChatResult(self._format_detail(result.appointment), True)

        result = self.appointment_service.list_user_appointments(
            actor_id,
            target_date=parsed.target_date,
            date_from=parsed.date_from,
            date_to=parsed.date_to,
        )
        if not result.success:
            return LifecycleChatResult(self._failure_message(result, "查询"), True)
        if not result.appointments:
            return LifecycleChatResult("没有找到符合条件的未来有效预约。", True)
        if len(result.appointments) == 1:
            return LifecycleChatResult(self._format_detail(result.appointments[0]), True)

        self._store_candidates(history, "get", result.appointments)
        return LifecycleChatResult(
            f"{self._format_candidates(result.appointments)}\n\n回复候选序号或预约编号可查看详情。",
            False,
        )

    def _start_cancel(
        self,
        parsed: ParsedLifecycleRequest,
        history: Dict[str, Any],
        actor_id: str,
    ) -> LifecycleChatResult:
        result = self._find_action_candidates(parsed, actor_id)
        if not result.success:
            return LifecycleChatResult(self._failure_message(result, "取消"), True)
        if not result.appointments:
            return LifecycleChatResult("没有找到可以取消的未来预约。", True)
        if len(result.appointments) > 1:
            self._store_candidates(history, "cancel", result.appointments)
            return LifecycleChatResult(
                f"请选择要取消的预约：\n\n{self._format_candidates(result.appointments)}",
                False,
            )
        return self._select_for_cancel(history, result.appointments[0])

    def _start_update(
        self,
        user_input: str,
        parsed: ParsedLifecycleRequest,
        history: Dict[str, Any],
        actor_id: str,
    ) -> LifecycleChatResult:
        selection_request = parsed
        if self._contains_change_target(user_input):
            selection_request = ParsedLifecycleRequest(
                intent=parsed.intent,
                appointment_id=parsed.appointment_id,
            )
            history["pending_changes"] = self._changes_from_parsed(parsed)

        result = self._find_action_candidates(selection_request, actor_id)
        if not result.success:
            return LifecycleChatResult(self._failure_message(result, "修改"), True)
        if not result.appointments:
            return LifecycleChatResult("没有找到可以修改的未来预约。", True)
        if len(result.appointments) > 1:
            self._store_candidates(history, "update", result.appointments)
            return LifecycleChatResult(
                f"请选择要修改的预约：\n\n{self._format_candidates(result.appointments)}",
                False,
            )
        self._set_selected(history, "update", result.appointments[0])
        if history.get("pending_changes"):
            return self._preview_update(history, actor_id)
        history["awaiting_lifecycle_changes"] = True
        return LifecycleChatResult(
            f"已选择：\n{self._format_detail(result.appointments[0])}\n\n"
            "请告诉我需要修改的日期、具体时间、发型师或服务。",
            False,
        )

    def _continue_flow(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> LifecycleChatResult:
        if is_abort_current_operation(user_input):
            self.clear_state(history)
            return LifecycleChatResult(
                "已退出本次预约操作，已有预约不会受到影响。",
                True,
            )
        if history.get("awaiting_lifecycle_selection"):
            return self._handle_selection(user_input, history, actor_id, now)
        action = history.get("pending_lifecycle_action")
        if is_bare_cancel(user_input) and not (
            action == "cancel" and history.get("awaiting_lifecycle_confirmation")
        ):
            self.clear_state(history)
            return LifecycleChatResult(
                "已退出本次预约操作，已有预约不会受到影响。",
                True,
            )
        if action == "cancel" and history.get("awaiting_lifecycle_confirmation"):
            return self._handle_cancel_confirmation(user_input, history, actor_id)
        if action == "update" and history.get("awaiting_lifecycle_changes"):
            return self._handle_update_changes(user_input, history, actor_id, now)
        if action == "update" and history.get("awaiting_lifecycle_confirmation"):
            return self._handle_update_confirmation(user_input, history, actor_id, now)
        self.clear_state(history)
        return LifecycleChatResult("当前预约操作状态已失效，请重新发起查询。", True)

    def _handle_selection(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> LifecycleChatResult:
        normalized = _normalize(user_input)
        if normalized in {"不用了", "退出", "取消操作", "取消"}:
            self.clear_state(history)
            return LifecycleChatResult(
                "已退出本次预约操作，已有预约不会受到影响。",
                True,
            )

        candidates = history.get("pending_appointment_candidates") or []
        matches = self._match_candidates(user_input, candidates, now)
        if not matches:
            return LifecycleChatResult("没有匹配到预约，请回复候选序号或预约编号。", False)
        if len(matches) > 1:
            return LifecycleChatResult(
                f"匹配到多笔预约，请继续指定序号：\n\n{self._format_candidates(matches)}",
                False,
            )

        action = history.get("pending_lifecycle_action")
        selected = matches[0]
        if action == "get":
            latest = self.appointment_service.get_user_appointment(
                selected["appointment_id"],
                actor_id,
            )
            self.clear_state(history)
            if not latest.success:
                return LifecycleChatResult(self._failure_message(latest, "查询"), True)
            return LifecycleChatResult(self._format_detail(latest.appointment), True)
        if action == "cancel":
            return self._select_for_cancel(history, selected)

        self._set_selected(history, "update", selected)
        if history.get("pending_changes"):
            return self._preview_update(history, actor_id)
        history["awaiting_lifecycle_changes"] = True
        return LifecycleChatResult(
            f"已选择：\n{self._format_detail(selected)}\n\n"
            "请告诉我需要修改的日期、具体时间、发型师或服务。",
            False,
        )

    def _select_for_cancel(
        self,
        history: Dict[str, Any],
        appointment: Dict[str, Any],
    ) -> LifecycleChatResult:
        self._set_selected(history, "cancel", appointment)
        history["awaiting_lifecycle_confirmation"] = True
        return LifecycleChatResult(
            f"请确认是否取消以下预约：\n{self._format_detail(appointment)}\n\n"
            "回复“确认取消预约”执行取消；回复“保留预约”退出本次操作。",
            False,
        )

    def _handle_cancel_confirmation(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
    ) -> LifecycleChatResult:
        normalized = _normalize(user_input)
        if normalized in {"保留预约", "不取消", "不用了", "退出", "取消"}:
            self.clear_state(history)
            return LifecycleChatResult("已保留原预约。", True)
        if normalized == "换一笔":
            history["awaiting_lifecycle_confirmation"] = False
            history["awaiting_lifecycle_selection"] = True
            return LifecycleChatResult(
                f"请选择其他预约：\n\n"
                f"{self._format_candidates(history.get('pending_appointment_candidates') or [])}",
                False,
            )
        if normalized not in {"确认取消预约", "确认", "是", "是的", "好的"}:
            return LifecycleChatResult(
                "请回复“确认取消预约”执行取消，或回复“保留预约”。",
                False,
            )

        result = self.appointment_service.cancel_appointment(
            int(history["selected_appointment_id"]),
            actor_id,
            int(history["selected_appointment_version"]),
        )
        self.clear_state(history)
        if result.status == "success":
            return LifecycleChatResult(
                f"预约已取消。\n{self._format_detail(result.appointment)}",
                True,
            )
        if result.status == "already_cancelled":
            return LifecycleChatResult("该预约已经取消，没有重复修改数据库。", True)
        return LifecycleChatResult(self._failure_message(result, "取消"), True)

    def _handle_update_changes(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> LifecycleChatResult:
        normalized = _normalize(user_input)
        if normalized in {"取消修改", "保留预约", "不用了", "退出"}:
            self.clear_state(history)
            return LifecycleChatResult("已退出修改流程，原预约保持不变。", True)

        parsed = self._parse(user_input, now)
        changes = history.setdefault("pending_changes", {})
        changes.update(self._changes_from_parsed(parsed))
        if parsed.period_label and parsed.target_time is None:
            return LifecycleChatResult(
                f"已记录{parsed.period_label}时段，请再提供一个具体时间，例如下午三点。",
                False,
            )
        if not changes:
            return LifecycleChatResult(
                "请提供新的日期、具体时间、发型师或服务。",
                False,
            )
        return self._preview_update(history, actor_id)

    def _preview_update(
        self,
        history: Dict[str, Any],
        actor_id: str,
    ) -> LifecycleChatResult:
        kwargs = self._change_kwargs(history.get("pending_changes") or {})
        result = self.appointment_service.preview_appointment_update(
            int(history["selected_appointment_id"]),
            actor_id,
            int(history["selected_appointment_version"]),
            **kwargs,
        )
        if result.status == "no_change":
            self.clear_state(history)
            return LifecycleChatResult("修改内容与当前预约相同，未写入数据库。", True)
        if not result.success:
            if result.status in {
                "conflict",
                "invalid_time",
                "outside_business_hours",
                "service_not_supported",
                "validation_error",
            }:
                history["pending_changes"] = {}
                history["awaiting_lifecycle_changes"] = True
                history["awaiting_lifecycle_confirmation"] = False
                return LifecycleChatResult(
                    f"{self._failure_message(result, '修改')} 请重新提供修改内容。",
                    False,
                )
            self.clear_state(history)
            return LifecycleChatResult(self._failure_message(result, "修改"), True)

        current = self.appointment_service.get_user_appointment(
            int(history["selected_appointment_id"]),
            actor_id,
        )
        if not current.success:
            self.clear_state(history)
            return LifecycleChatResult(self._failure_message(current, "修改"), True)
        history["awaiting_lifecycle_changes"] = False
        history["awaiting_lifecycle_confirmation"] = True
        return LifecycleChatResult(
            "请确认以下修改：\n\n"
            f"原预约：{self._summary_line(current.appointment)}\n"
            f"新预约：{self._summary_line(result.appointment)}\n\n"
            "回复“确认修改”执行修改；回复“保留预约”退出。",
            False,
        )

    def _handle_update_confirmation(
        self,
        user_input: str,
        history: Dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> LifecycleChatResult:
        normalized = _normalize(user_input)
        if normalized in {"取消修改", "保留预约", "不用了", "取消", "退出"}:
            self.clear_state(history)
            return LifecycleChatResult("已保留原预约。", True)
        if normalized not in {"确认修改", "确认", "是", "是的", "好的"}:
            history["awaiting_lifecycle_confirmation"] = False
            history["awaiting_lifecycle_changes"] = True
            return self._handle_update_changes(user_input, history, actor_id, now)

        result = self.appointment_service.update_appointment(
            int(history["selected_appointment_id"]),
            actor_id,
            int(history["selected_appointment_version"]),
            **self._change_kwargs(history.get("pending_changes") or {}),
        )
        self.clear_state(history)
        if result.status == "success":
            return LifecycleChatResult(
                f"预约修改成功，预约编号保持为 {result.appointment['appointment_id']}。\n"
                f"{self._format_detail(result.appointment)}",
                True,
            )
        if result.status == "no_change":
            return LifecycleChatResult("预约信息没有变化，未写入数据库。", True)
        return LifecycleChatResult(self._failure_message(result, "修改"), True)

    def _find_action_candidates(
        self,
        parsed: ParsedLifecycleRequest,
        actor_id: str,
    ) -> AppointmentLifecycleResult:
        if parsed.appointment_id is not None:
            result = self.appointment_service.get_user_appointment(
                parsed.appointment_id,
                actor_id,
            )
            if not result.success:
                return result
            return AppointmentLifecycleResult(
                True,
                "success",
                appointments=(result.appointment,),
            )
        return self.appointment_service.list_user_appointments(
            actor_id,
            target_date=parsed.target_date,
            date_from=parsed.date_from,
            date_to=parsed.date_to,
        )

    @staticmethod
    def _store_candidates(
        history: Dict[str, Any],
        action: str,
        appointments,
    ) -> None:
        history["pending_lifecycle_action"] = action
        history["pending_appointment_candidates"] = [
            AppointmentLifecycleProcessor._candidate_snapshot(item)
            for item in appointments
        ]
        history["awaiting_lifecycle_selection"] = True

    @staticmethod
    def _set_selected(
        history: Dict[str, Any],
        action: str,
        appointment: Dict[str, Any],
    ) -> None:
        history["pending_lifecycle_action"] = action
        history["selected_appointment_id"] = int(appointment["appointment_id"])
        history["selected_appointment_version"] = int(appointment["version"])
        history["awaiting_lifecycle_selection"] = False

    @staticmethod
    def _candidate_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
        # This display snapshot is never trusted for a write; final operations re-read SQLite.
        return {
            "appointment_id": int(item["appointment_id"]),
            "version": int(item["version"]),
            "stylist_id": int(item["stylist_id"]),
            "stylist_name": item["stylist_name"],
            "service_name": item["service_name"],
            "start_time": item["start_time"],
            "end_time": item["end_time"],
            "status": item["status"],
            "price": item.get("price"),
            "duration_minutes": item.get("duration_minutes"),
        }

    @staticmethod
    def _match_candidates(
        text: str,
        candidates: list[Dict[str, Any]],
        now: datetime,
    ) -> list[Dict[str, Any]]:
        normalized = _normalize(text)
        ordinal_map = {
            "第一个": 1,
            "第一": 1,
            "第二个": 2,
            "第二": 2,
            "第三个": 3,
            "第三": 3,
        }
        index = ordinal_map.get(normalized)
        number_match = re.fullmatch(r"(?:选|第)?(\d+)(?:个|笔)?", normalized)
        if index is None and number_match:
            index = int(number_match.group(1))
        if index is not None:
            return [candidates[index - 1]] if 0 < index <= len(candidates) else []

        id_match = re.search(r"预约(?:编号|id|ID)?[：:#]?([1-9]\d*)", normalized)
        if id_match:
            appointment_id = int(id_match.group(1))
            return [item for item in candidates if item["appointment_id"] == appointment_id]

        matches = list(candidates)
        temporal = parse_booking_temporal_slots(text, now=now)
        if temporal.target_date:
            matches = [
                item for item in matches
                if _as_datetime(item["start_time"]).date() == temporal.target_date
            ]
        if temporal.exact_time:
            matches = [
                item for item in matches
                if _as_datetime(item["start_time"]).time().replace(second=0, microsecond=0)
                == temporal.exact_time
            ]
        stylist_matches = [
            item for item in matches
            if item.get("stylist_name") and item["stylist_name"] in text
        ]
        return stylist_matches or (matches if len(matches) < len(candidates) else [])

    @staticmethod
    def _changes_from_parsed(parsed: ParsedLifecycleRequest) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        if parsed.target_date:
            changes["target_date"] = parsed.target_date.isoformat()
        if parsed.target_time:
            changes["target_time"] = parsed.target_time.strftime("%H:%M")
        if parsed.stylist_name:
            changes["stylist_name"] = parsed.stylist_name
        if parsed.service_value:
            changes["service_value"] = parsed.service_value
        return changes

    @staticmethod
    def _change_kwargs(changes: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "target_date": (
                date.fromisoformat(changes["target_date"])
                if changes.get("target_date")
                else None
            ),
            "target_time": (
                time.fromisoformat(changes["target_time"])
                if changes.get("target_time")
                else None
            ),
            "stylist_name": changes.get("stylist_name"),
            "service_value": changes.get("service_value"),
        }

    @staticmethod
    def _contains_change_target(text: str) -> bool:
        return bool(re.search(r"改到|换到|挪到|延到|改成|换成|改为|换为", text or ""))

    @staticmethod
    def _format_candidates(appointments) -> str:
        lines = []
        for index, item in enumerate(appointments, start=1):
            lines.append(f"{index}. {AppointmentLifecycleProcessor._summary_line(item)}")
        return "\n".join(lines)

    @staticmethod
    def _format_detail(item: Dict[str, Any]) -> str:
        start = _as_datetime(item["start_time"])
        end = _as_datetime(item["end_time"])
        return (
            f"预约编号：{item['appointment_id']}\n"
            f"时间：{start:%Y-%m-%d %H:%M}–{end:%H:%M}\n"
            f"发型师：{item['stylist_name']}\n"
            f"服务：{item['service_name']}\n"
            f"状态：{appointment_status_label(item.get('status'))}"
        )

    @staticmethod
    def _summary_line(item: Dict[str, Any]) -> str:
        start = _as_datetime(item["start_time"])
        return (
            f"预约{item['appointment_id']}，{start:%Y-%m-%d %H:%M}，"
            f"{item['stylist_name']}，{item['service_name']}，"
            f"状态 {appointment_status_label(item.get('status'))}"
        )

    @staticmethod
    def _failure_message(result: AppointmentLifecycleResult, operation: str) -> str:
        if result.reason == "stylist_not_found":
            return "没有找到指定发型师，原预约保持不变。"
        messages = {
            "not_found": "没有找到符合条件的预约。",
            "already_cancelled": "该预约已经取消。",
            "not_modifiable": "该预约已经开始、完成或取消，当前不能操作。",
            "stale_state": "预约状态已变化，请重新查询后再操作。",
            "conflict": "目标档期已被占用，原预约保持不变。",
            "invalid_time": "目标时间已经过去，请选择未来时间。",
            "outside_business_hours": "目标时间不在门店营业时间内。",
            "service_not_supported": "目标发型师不支持该服务。",
            "validation_error": "修改内容无法识别，请重新说明。",
            "persistence_error": "系统暂时无法完成操作，数据库未保留半完成修改。",
        }
        return messages.get(result.status, f"暂时无法完成预约{operation}。")


def _normalize(text: str) -> str:
    return re.sub(r"[，。！？,.!?\s]+$", "", (text or "").strip().lower())


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))

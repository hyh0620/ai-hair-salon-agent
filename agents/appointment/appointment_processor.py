"""
预约处理器

负责协调整个预约流程
"""

import os
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Any, AsyncGenerator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .input_parser import InputParser
from .stylist_finder import StylistFinder
from .message_builder import MessageBuilder
from .appointment_database import AppointmentDatabase
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr
from agents.appointment.availability_parser import ParsedAvailabilityRequest, parse_selection_time
from config.time_config import time_config
from services.availability_service import AvailabilitySearchRequest, AvailabilityService
from services.service_catalog import SERVICE_CATALOG, normalize_service

logger = logging.getLogger(__name__)

POSITIVE_CONFIRMATIONS = {
    "确认", "好的", "好", "可以", "是", "是的", "没问题", "同意",
    "确定", "yes", "ok", "行", "就他", "就这个", "预约他",
}
NEGATIVE_CONFIRMATIONS = {
    "取消", "不用了", "不确认", "不", "不要", "不行", "不同意",
    "换", "换一个", "换其他发型师", "no",
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class WeatherContextResult:
    """Result from the optional external weather context lookup."""

    status: str
    context: str = ""
    reason: str = ""
    http_status: Optional[int] = None
    forecast_time: Optional[datetime] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    precipitation_probability: Optional[float] = None
    precipitation: Optional[float] = None
    weather_code: Optional[int] = None


class WeatherTool(BaseTool):
    """Optional external weather context tool for post-booking travel reminders."""

    name: str = "get_booking_weather_forecast"
    description: str = "获取预约开始时间对应的上海天气预报，用于预约成功后的可选出行提醒"
    _enabled: bool = PrivateAttr(default=True)
    _provider: str = PrivateAttr(default="open_meteo")
    _location_name: str = PrivateAttr(default="上海")
    _latitude: float = PrivateAttr(default=31.2304)
    _longitude: float = PrivateAttr(default=121.4737)
    _timezone_name: str = PrivateAttr(default="Asia/Shanghai")
    _forecast_days: int = PrivateAttr(default=16)
    _timeout_seconds: float = PrivateAttr(default=3.0)
    _base_url: str = PrivateAttr(default="https://api.open-meteo.com/v1/forecast")
    
    def __init__(self):
        super().__init__()
        self._enabled = _env_bool("WEATHER_ENABLED", True)
        self._provider = (os.getenv("WEATHER_PROVIDER") or "open_meteo").strip().lower()
        self._location_name = (os.getenv("WEATHER_LOCATION_NAME") or "上海").strip() or "上海"
        self._latitude = _env_float("WEATHER_LATITUDE", 31.2304)
        self._longitude = _env_float("WEATHER_LONGITUDE", 121.4737)
        self._timezone_name = (os.getenv("WEATHER_TIMEZONE") or "Asia/Shanghai").strip()
        self._timeout_seconds = _env_float("WEATHER_TIMEOUT_SECONDS", 3.0)
        self._forecast_days = max(1, min(_env_int("WEATHER_FORECAST_DAYS", 16), 16))

    @property
    def is_configured(self) -> bool:
        return bool(
            self._enabled
            and self._provider == "open_meteo"
            and self._timezone_name
            and -90 <= self._latitude <= 90
            and -180 <= self._longitude <= 180
        )

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def location_name(self) -> str:
        return self._location_name

    def omission_reason(self) -> Optional[str]:
        if not self._enabled:
            return "disabled"
        if self._provider != "open_meteo":
            return "unsupported_provider"
        if not self._timezone_name:
            return "invalid_appointment_time"
        return None

    async def get_weather_context(
        self,
        appointment_time: datetime | str | None = None,
    ) -> WeatherContextResult:
        """Fetch Shanghai's hourly forecast nearest to the appointment start."""
        reason = self.omission_reason()
        if reason:
            self._log_result(appointment_time, None, "omitted", reason)
            return WeatherContextResult(status="omitted", reason=reason)

        parsed_time = self._parse_appointment_time(appointment_time)
        if parsed_time is None:
            self._log_result(appointment_time, None, "omitted", "invalid_appointment_time")
            return WeatherContextResult(status="omitted", reason="invalid_appointment_time")
        if parsed_time < self._now() - timedelta(minutes=5):
            self._log_result(parsed_time, None, "omitted", "past_appointment")
            return WeatherContextResult(status="omitted", reason="past_appointment")

        try:
            import aiohttp

            params = {
                "latitude": self._latitude,
                "longitude": self._longitude,
                "hourly": ",".join([
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "precipitation_probability",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]),
                "timezone": self._timezone_name,
                "forecast_days": self._forecast_days,
            }
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._base_url, params=params) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                        except Exception:
                            self._log_result(parsed_time, None, "unavailable", "malformed_response")
                            return WeatherContextResult(status="unavailable", reason="malformed_response")
                        result = self._select_forecast(parsed_time, data)
                        self._log_result(parsed_time, result.forecast_time, result.status, result.reason)
                        return result

                    reason = "http_4xx" if 400 <= response.status < 500 else "http_5xx"
                    self._log_result(parsed_time, None, "unavailable", reason)
                    return WeatherContextResult(
                        status="unavailable",
                        reason=reason,
                        http_status=response.status,
                    )
        except asyncio.TimeoutError:
            self._log_result(parsed_time, None, "unavailable", "timeout")
            return WeatherContextResult(status="unavailable", reason="timeout")
        except Exception:
            self._log_result(parsed_time, None, "unavailable", "network_error")
            return WeatherContextResult(status="unavailable", reason="network_error")

    def _select_forecast(self, appointment_time: datetime, data: Dict[str, Any]) -> WeatherContextResult:
        try:
            hourly = data.get("hourly") or {}
            raw_times = hourly.get("time") or []
            timezone_info = ZoneInfo(self._timezone_name)
            forecast_times = [
                datetime.fromisoformat(value).replace(tzinfo=timezone_info)
                for value in raw_times
            ]
            required = (
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation_probability",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
            )
            if not forecast_times or any(len(hourly.get(field) or []) != len(forecast_times) for field in required):
                return WeatherContextResult(status="unavailable", reason="malformed_response")

            horizon_end = forecast_times[-1] + timedelta(hours=1)
            if appointment_time < forecast_times[0] or appointment_time >= horizon_end:
                return WeatherContextResult(status="omitted", reason="outside_forecast_horizon")

            index = min(
                range(len(forecast_times)),
                key=lambda item: abs((forecast_times[item] - appointment_time).total_seconds()),
            )
            selected_time = forecast_times[index]
            temperature = float(hourly["temperature_2m"][index])
            apparent_temperature = float(hourly["apparent_temperature"][index])
            humidity = float(hourly["relative_humidity_2m"][index])
            precipitation_probability = float(hourly["precipitation_probability"][index])
            precipitation = float(hourly["precipitation"][index])
            weather_code = int(hourly["weather_code"][index])
            wind_speed = float(hourly["wind_speed_10m"][index])
            description = self._weather_code_description(weather_code)
            context = (
                f"天气提醒：预计预约时段{self._location_name}{description}，"
                f"气温{_format_number(temperature)}°C，体感{_format_number(apparent_temperature)}°C，"
                f"降水概率{_format_number(precipitation_probability)}%，"
                f"湿度{_format_number(humidity)}%，风速{_format_number(wind_speed)}km/h。"
            )
            return WeatherContextResult(
                status="available",
                context=context,
                forecast_time=selected_time,
                temperature=temperature,
                humidity=humidity,
                precipitation_probability=precipitation_probability,
                precipitation=precipitation,
                weather_code=weather_code,
            )
        except (TypeError, ValueError, KeyError, IndexError, ZoneInfoNotFoundError):
            return WeatherContextResult(status="unavailable", reason="malformed_response")

    def _parse_appointment_time(self, value: datetime | str | None) -> Optional[datetime]:
        try:
            timezone_info = ZoneInfo(self._timezone_name)
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, str) and value.strip():
                parsed = datetime.fromisoformat(value.strip())
            else:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone_info)
            return parsed.astimezone(timezone_info)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return None

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self._timezone_name))

    @staticmethod
    def _weather_code_description(code: int) -> str:
        if code == 0:
            return "晴"
        if 1 <= code <= 3:
            return "多云"
        if code in {45, 48}:
            return "有雾"
        if 51 <= code <= 57:
            return "有毛毛雨"
        if 61 <= code <= 67:
            return "有雨"
        if 71 <= code <= 77:
            return "有雪"
        if 80 <= code <= 82:
            return "有阵雨"
        if code in {85, 86}:
            return "有阵雪"
        if code == 95:
            return "有雷暴"
        if code in {96, 99}:
            return "有雷暴伴冰雹"
        return "天气情况待确认"

    def _log_result(
        self,
        appointment_time: datetime | str | None,
        forecast_time: Optional[datetime],
        status: str,
        reason: str,
    ) -> None:
        logger.info(
            "weather_forecast provider=%s location=Shanghai appointment_time=%s "
            "selected_forecast_time=%s status=%s reason=%s",
            self._provider,
            appointment_time.isoformat() if isinstance(appointment_time, datetime) else appointment_time,
            forecast_time.isoformat() if forecast_time else None,
            status,
            reason or "none",
        )

    def _run(self, appointment_time: datetime | str | None = None) -> str:
        """Synchronous wrapper used by LangChain Tool interfaces."""
        return asyncio.run(self.get_weather_context(appointment_time)).context
    
    async def _arun(self, appointment_time: datetime | str | None = None) -> str:
        """Async wrapper used by LangChain Tool interfaces."""
        return (await self.get_weather_context(appointment_time)).context


def _format_number(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.1f}".rstrip("0").rstrip(".")


class AppointmentProcessor:
    """预约处理器"""
    
    def __init__(self, input_parser: InputParser, stylist_finder: StylistFinder,
                 message_builder: MessageBuilder, appointment_database: AppointmentDatabase, llm=None):
        self.input_parser = input_parser
        self.stylist_finder = stylist_finder
        self.message_builder = message_builder
        self.appointment_database = appointment_database
        self.llm = llm
        self.weather_tool = WeatherTool()
        self.availability_service = (
            AvailabilityService(appointment_database.appointment_service)
            if appointment_database is not None
            else None
        )
    
    def update_history_from_data(self, appointment_history: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """从解析数据更新预约历史"""
        if appointment_history.get('awaiting_confirmation'):
            if self._is_new_appointment_request(data):
                logger.info("appointment_pending_replaced_by_new_request")
                self.clear_pending_recommendation(appointment_history)
            elif self._is_explicit_confirmation(data):
                return self._handle_recommendation_response(appointment_history, data)
            else:
                return False
        
        # 只更新有值的字段，避免覆盖之前的信息
        for key in [
            "duration",
            "gender",
            "start_time",
            "project",
            "stylist_name",
            "preference",
            "style_preference",
            "budget",
        ]:
            if data.get(key) and data[key] != "未知":
                appointment_history[key] = data[key]

        service = normalize_service(appointment_history.get("project"))
        if service:
            appointment_history["project"] = service.name
            appointment_history["service_key"] = service.key
            appointment_history["price"] = service.standard_price
            if not appointment_history.get("duration") or appointment_history.get("duration") == "未知":
                appointment_history["duration"] = f"{service.standard_duration}分钟"

        return self.has_required_fields(appointment_history)

    @staticmethod
    def _present(value: Any) -> bool:
        return value not in (None, "", "未知")

    def has_required_fields(self, appointment_history: Dict[str, Any]) -> bool:
        return not self.get_missing_fields(appointment_history)

    def get_missing_fields(self, appointment_history: Dict[str, Any]) -> list[str]:
        required_fields = ("start_time", "project", "duration")
        return [field for field in required_fields if not self._present(appointment_history.get(field))]

    def _is_new_appointment_request(self, data: Dict[str, Any]) -> bool:
        return self._present(data.get("start_time")) and self._present(data.get("project"))

    @staticmethod
    def _is_explicit_confirmation(data: Dict[str, Any]) -> bool:
        return AppointmentProcessor.is_explicit_confirmation_text(data.get("confirmation"))

    @staticmethod
    def is_explicit_confirmation_text(value: Any) -> bool:
        response = str(value or "").strip().lower()
        response = response.rstrip("，。！？,.!?")
        return response in POSITIVE_CONFIRMATIONS | NEGATIVE_CONFIRMATIONS

    @staticmethod
    def clear_pending_recommendation(appointment_history: Dict[str, Any]) -> None:
        for key in (
            "awaiting_confirmation",
            "recommended_stylist",
            "original_stylist",
            "confirmed_stylist",
            "recommendation_declined",
        ):
            appointment_history.pop(key, None)

    @staticmethod
    def clear_pending_availability(appointment_history: Dict[str, Any]) -> None:
        for key in (
            "availability_search_active",
            "awaiting_slot_selection",
            "pending_availability_options",
            "awaiting_slot_confirmation",
            "selected_availability_option",
            "availability_date",
            "availability_range_start",
            "availability_range_end",
            "availability_exact_time",
            "availability_period_label",
            "availability_time_text",
            "specialty",
        ):
            appointment_history.pop(key, None)

    async def handle_availability_search(
        self,
        parsed: ParsedAvailabilityRequest,
        appointment_history: Dict[str, Any],
        session_id: str,
        now: Optional[datetime] = None,
    ) -> AsyncGenerator[str, None]:
        """Merge fuzzy slots, query persisted schedules, and retain structured candidates."""
        now = now or time_config.now()
        appointment_history["availability_search_active"] = True
        if parsed.target_date:
            appointment_history["availability_date"] = parsed.target_date.isoformat()
        if parsed.range_start:
            appointment_history["availability_range_start"] = parsed.range_start.strftime("%H:%M")
        if parsed.range_end:
            appointment_history["availability_range_end"] = parsed.range_end.strftime("%H:%M")
        if parsed.exact_time:
            appointment_history["availability_exact_time"] = parsed.exact_time.strftime("%H:%M")
        if parsed.period_label:
            appointment_history["availability_period_label"] = parsed.period_label
        if parsed.service_key:
            service = SERVICE_CATALOG[parsed.service_key]
            appointment_history.update({
                "service_key": service.key,
                "project": service.name,
                "duration": f"{service.standard_duration}分钟",
                "price": service.standard_price,
            })
        if parsed.specialty:
            appointment_history["specialty"] = parsed.specialty
            appointment_history["preference"] = parsed.specialty
        if parsed.stylist_name:
            appointment_history["stylist_name"] = parsed.stylist_name

        missing = []
        if not appointment_history.get("service_key"):
            missing.append("service")
        if not appointment_history.get("availability_date"):
            missing.append("date")
        if not appointment_history.get("availability_range_start") and not appointment_history.get("availability_exact_time"):
            missing.append("time")
        if missing:
            prompts = {
                "service": "您想预约剪发、染发、烫发还是其他服务？",
                "date": "请告诉我希望预约哪一天。",
                "time": "请告诉我希望预约上午、下午、晚上或具体几点。",
            }
            logger.info(
                "availability_search_incomplete session_id=%s service=%s specialty=%s missing=%s",
                session_id,
                appointment_history.get("service_key"),
                appointment_history.get("specialty"),
                ",".join(missing),
            )
            yield f"[REPLY][预约机器人]{' '.join(prompts[item] for item in missing)}"
            return

        target_date = datetime.strptime(appointment_history["availability_date"], "%Y-%m-%d").date()
        exact_text = appointment_history.get("availability_exact_time")
        exact_time = time.fromisoformat(exact_text) if exact_text else None
        range_start = time.fromisoformat(
            appointment_history.get("availability_range_start") or exact_text
        )
        range_end = time.fromisoformat(
            appointment_history.get("availability_range_end") or exact_text
        )
        if target_date < now.date():
            yield "[REPLY][预约机器人]该日期已经过去，请选择今天之后的预约日期。"
            return

        search_request = AvailabilitySearchRequest(
            target_date=target_date,
            range_start=range_start,
            range_end=range_end,
            exact_time=exact_time,
            service_key=appointment_history["service_key"],
            specialty=appointment_history.get("specialty"),
            stylist_name=appointment_history.get("stylist_name"),
        )
        matching_stylists = self.availability_service.matching_stylists(search_request)
        options = self.availability_service.search_available_stylists(search_request, now=now)
        logger.info(
            "availability_search session_id=%s intent=search_availability date=%s time_range=%s-%s "
            "service=%s specialty=%s candidate_count=%s",
            session_id,
            target_date,
            range_start,
            range_end,
            appointment_history.get("service_key"),
            appointment_history.get("specialty"),
            len(options),
        )
        if not options:
            specialty = appointment_history.get("specialty")
            if specialty and not matching_stylists:
                yield (
                    f"[REPLY][预约机器人]当前发型师资料中没有标记为擅长“{specialty}”"
                    "且支持该服务的老师。您可以调整偏好后重新查询。"
                )
            elif specialty:
                yield (
                    f"[REPLY][预约机器人]{target_date.isoformat()}"
                    f"{appointment_history.get('availability_period_label') or '所选时段'}，"
                    f"当前没有同时匹配“{specialty}”专长且具备完整"
                    f"{appointment_history['duration']}空档的发型师。请调整日期、时间或偏好。"
                )
            else:
                yield (
                    f"[REPLY][预约机器人]{target_date.isoformat()}"
                    f"{appointment_history.get('availability_period_label') or '所选时段'}"
                    f"暂时没有完整的{appointment_history['duration']}空档，请调整时间。"
                )
            return

        session_options = [item.to_session_dict() for item in options]
        appointment_history["pending_availability_options"] = session_options
        appointment_history["awaiting_slot_selection"] = True
        appointment_history["availability_time_text"] = (
            appointment_history.get("availability_period_label")
            or appointment_history.get("availability_exact_time")
        )
        yield f"[REPLY][预约机器人]{self.message_builder.create_availability_options_message(session_options, appointment_history)}"

    async def handle_availability_selection(
        self,
        user_input: str,
        appointment_history: Dict[str, Any],
        session_id: str,
    ) -> AsyncGenerator[str, None]:
        normalized = str(user_input or "").strip().lower().rstrip("，。！？,.!?")
        if normalized == "换一批":
            yield "[REPLY][预约机器人]当前匹配候选已全部展示。请调整日期、时间或偏好后重新查询。"
            return
        if normalized in NEGATIVE_CONFIRMATIONS or normalized == "都不合适":
            self.clear_pending_availability(appointment_history)
            appointment_history["availability_flow_complete"] = True
            yield "[REPLY][预约机器人]已取消本次候选选择。您可以告诉我新的日期、时间或服务偏好。"
            return

        options = appointment_history.get("pending_availability_options") or []
        matches = self._match_availability_options(normalized, options)
        if not matches:
            yield "[REPLY][预约机器人]没有匹配到该选项，请回复候选序号，或使用“发型师姓名+时间”。"
            return
        if len(matches) > 1:
            yield f"[REPLY][预约机器人]{self.message_builder.create_ambiguous_option_message(matches)}"
            return

        option = matches[0]
        appointment_history["selected_availability_option"] = option
        appointment_history["awaiting_slot_selection"] = False
        appointment_history["awaiting_slot_confirmation"] = True
        logger.info(
            "availability_option_selected session_id=%s option_id=%s stylist_id=%s",
            session_id,
            option["option_id"],
            option["stylist_id"],
        )
        yield f"[REPLY][预约机器人]{self.message_builder.create_availability_confirmation_message(option)}"

    async def handle_availability_confirmation(
        self,
        user_input: str,
        appointment_history: Dict[str, Any],
        session_id: str,
    ) -> AsyncGenerator[str, None]:
        normalized = str(user_input or "").strip().lower().rstrip("，。！？,.!?")
        if normalized in NEGATIVE_CONFIRMATIONS:
            self.clear_pending_availability(appointment_history)
            appointment_history["availability_flow_complete"] = True
            yield "[REPLY][预约机器人]已取消本次预约，没有写入排班。"
            return
        if normalized not in POSITIVE_CONFIRMATIONS:
            yield "[REPLY][预约机器人]请回复“确认”完成预约，或回复“取消”。"
            return

        option = appointment_history.get("selected_availability_option")
        if not option:
            self.clear_pending_availability(appointment_history)
            appointment_history["availability_flow_complete"] = True
            yield "[REPLY][预约机器人]候选状态已失效，请重新查询可预约时间。"
            return

        stylist = self.appointment_database.appointment_service.get_stylist_by_id(option["stylist_id"])
        if not stylist:
            self.clear_pending_availability(appointment_history)
            appointment_history["availability_flow_complete"] = True
            yield "[REPLY][预约机器人]该发型师信息已不可用，请重新查询。"
            return

        appointment_history.update({
            "start_time": datetime.fromisoformat(option["start_time"]).strftime("%Y-%m-%d %H:%M"),
            "project": option["service_name"],
            "service_key": option["service_key"],
            "duration": f"{option['duration_minutes']}分钟",
            "price": option["price"],
            "stylist_name": option["stylist_name"],
        })
        appointment_history["awaiting_slot_confirmation"] = False
        reply = await self._process_successful_appointment(stylist, appointment_history, session_id)
        success = bool(appointment_history.get("appointment_id"))
        logger.info(
            "availability_booking session_id=%s option_id=%s booking_status=%s reason=%s",
            session_id,
            option["option_id"],
            "confirmed" if success else "failed",
            "" if success else "slot_conflict_or_persistence_error",
        )
        self.clear_pending_availability(appointment_history)
        appointment_history["availability_flow_complete"] = True
        if not success:
            yield "[REPLY][预约机器人]该候选档期刚刚变得不可用，没有创建预约。请重新查询其他时间。"
            return
        yield f"[REPLY][预约机器人]{reply}"

    @staticmethod
    def _match_availability_options(user_input: str, options: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        ordinal_map = {"第一个": 1, "第一": 1, "第二个": 2, "第二": 2, "第三个": 3, "第三": 3}
        option_number = ordinal_map.get(user_input)
        if option_number is None:
            number_match = re.fullmatch(r"(?:选|第)?\s*(\d+)(?:个)?", user_input)
            option_number = int(number_match.group(1)) if number_match else None
        if option_number is not None:
            return [item for item in options if item.get("option_id") == option_number]

        matches = [item for item in options if item.get("stylist_name") and item["stylist_name"] in user_input]
        selected_time = parse_selection_time(user_input)
        if selected_time:
            matches = [
                item for item in (matches or options)
                if datetime.fromisoformat(item["start_time"]).time().replace(second=0, microsecond=0) == selected_time
            ]
        return matches

    def _handle_recommendation_response(self, appointment_history: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """处理用户对推荐发型师的回应"""
        user_response = str(data.get('confirmation') or '').strip().lower().rstrip("，。！？,.!?")
        
        # 判断用户是否同意推荐
        is_positive = user_response in POSITIVE_CONFIRMATIONS
        is_negative = user_response in NEGATIVE_CONFIRMATIONS
        
        if is_positive and not is_negative:
            # 用户同意推荐，更新发型师信息
            recommended_stylist = appointment_history.get('recommended_stylist')
            if recommended_stylist:
                appointment_history['confirmed_stylist'] = recommended_stylist
                appointment_history['awaiting_confirmation'] = False
                return True  # 表示可以进行预约
        elif is_negative:
            # 用户拒绝推荐
            appointment_history['recommendation_declined'] = True
            appointment_history['awaiting_confirmation'] = False
            return True  # 表示需要处理拒绝情况
        
        # 用户回应不明确，继续等待
        # 这里返回 False，表示信息还不完整，需要继续等待用户输入
        return False
    
    async def handle_unrelated_request(self, user_input: str, unrelated_callback, state) -> AsyncGenerator[str, None]:
        """处理与预约无关的请求"""
        # 注意：这里不重置状态，因为在调用处已经设置了状态
        # 保持预约历史不被清空
        
        if unrelated_callback:
            try:
                yield "[REPLY][预约机器人]和预约信息无关，已交给归类机器人处理\n"
                result = await unrelated_callback(user_input)
                if hasattr(result, '__aiter__'):
                    async for token in result:
                        yield token
                else:
                    yield result
            except Exception as e:
                yield f"[ERROR]处理请求时发生错误: {str(e)}\n"
                yield self.message_builder.create_unrelated_message()
        else:
            yield self.message_builder.create_unrelated_message()
    
    async def handle_complete_appointment(self, appointment_history: Dict[str, Any], 
                                        session_id: str) -> AsyncGenerator[str, None]:
        """处理预约信息完整的情况"""
        # 检查是否用户拒绝了推荐
        if appointment_history.get('recommendation_declined'):
            reply = self.message_builder.create_recommendation_declined_message(self.llm)
            yield f"[REPLY][预约机器人]{reply}"
            # 清理状态
            appointment_history.pop('recommendation_declined', None)
            appointment_history.pop('recommended_stylist', None)
            appointment_history.pop('original_stylist', None)
            return
        
        # 检查是否用户确认了推荐发型师
        if appointment_history.get('confirmed_stylist'):
            stylist = appointment_history['confirmed_stylist']
            # 标记为推荐发型师用于成功消息显示
            stylist['is_recommendation'] = True
            stylist['original_stylist'] = appointment_history.get('original_stylist')
            reply = await self._process_successful_appointment(stylist, appointment_history, session_id)
            yield f"[REPLY][预约机器人]{reply}"
            # 清理状态
            appointment_history.pop('confirmed_stylist', None)
            appointment_history.pop('recommended_stylist', None)
            appointment_history.pop('original_stylist', None)
            return
        
        # 检查是否在等待用户确认推荐发型师
        if appointment_history.get('awaiting_confirmation'):
            # 用户回应不明确，重新询问
            yield f"[REPLY][预约机器人]\n机器人：请您明确回复\"是\"或\"不\"，我好为您安排预约。\n"
            return
        
        # 收集思考过程
        thought_msgs = []
        def collect_thoughts(msg):
            thought_msgs.append(msg)
        
        stylist = self.stylist_finder.find_stylist_with_thought(appointment_history, collect_thoughts)
        
        # 输出所有思考过程
        for msg in thought_msgs:
            yield msg
        
        stylist_name = appointment_history.get("stylist_name")
        
        if stylist:
            # 检查是否是需要确认的推荐
            if stylist.get('requires_confirmation'):
                original_stylist = stylist.get('original_stylist')
                recommended_stylist = stylist.get('recommended_stylist')
                
                # 生成推荐消息
                recommendation_msg = self.message_builder.create_stylist_recommendation_message(
                    original_stylist, recommended_stylist, appointment_history, self.llm
                )
                yield f"[REPLY][预约机器人]{recommendation_msg}"
                
                # 将推荐信息存储在预约历史中，等待用户确认
                appointment_history['recommended_stylist'] = recommended_stylist
                appointment_history['original_stylist'] = original_stylist
                appointment_history['awaiting_confirmation'] = True
                
                # 重要：告诉调用方这个预约还没有真正完成，需要继续等待用户输入
                yield "[SIGNAL]recommendation_pending"
                return
            else:
                # 正常预约流程
                reply = await self._process_successful_appointment(stylist, appointment_history, session_id)
                yield f"[REPLY][预约机器人]{reply}"
        else:
            reply = self.message_builder.create_appointment_failure_message(stylist_name)
            yield f"[REPLY][预约机器人]{reply}"
    
    async def _process_successful_appointment(self, stylist: Dict[str, Any],
                                           appointment_history: Dict[str, Any], session_id: str) -> str:
        """处理预约成功的情况，并在配置完整时追加可选天气出行提醒。"""
        details = self.appointment_database.appointment_service.build_appointment_details(appointment_history)
        appointment_history.update(details)
        start_time, end_time, duration_min = self.stylist_finder.parse_time_and_duration(
            appointment_history["start_time"], 
            appointment_history["duration"]
        )
        # 保存预约到数据库
        saved = self.appointment_database.save_appointment_detailed(
            stylist["id"], start_time, end_time, appointment_history, session_id
        )
        if saved.success:
            appointment_history["appointment_id"] = saved.appointment_id
            appointment_history["schedule_id"] = saved.schedule_id
            appointment_history["start_time"] = start_time.strftime("%Y-%m-%d %H:%M")
            # 更新内存中的忙碌时段
            self.appointment_database.update_memory_schedule(stylist["id"], start_time, end_time)
            base_message = self.message_builder.create_appointment_success_message(stylist, appointment_history)
            weather_reminder = await self._create_optional_weather_reminder(appointment_history)
            return f"{base_message}{weather_reminder}" if weather_reminder else base_message
        else:
            return self.message_builder.create_save_failure_message()

    async def _create_optional_weather_reminder(self, appointment_history: Dict[str, Any]) -> str:
        try:
            result = await self.weather_tool.get_weather_context(appointment_history.get("start_time"))
        except Exception as exc:
            logger.warning("weather_context_unavailable reason=%s", type(exc).__name__)
            appointment_history["weather_status"] = "unavailable"
            appointment_history["weather_unavailable_reason"] = type(exc).__name__
            return ""

        appointment_history["weather_status"] = result.status
        if result.reason:
            appointment_history["weather_unavailable_reason"] = result.reason
        if result.status != "available" or not result.context:
            return ""

        tip = self._weather_care_tip(appointment_history.get("project", ""), result)
        return f"{result.context}{tip}\n"

    @staticmethod
    def _weather_care_tip(project: str, result: WeatherContextResult) -> str:
        rain_codes = set(range(51, 68)) | set(range(80, 83)) | {95, 96, 99}
        rain_risk = bool(
            (result.precipitation_probability or 0) >= 30
            or (result.precipitation or 0) > 0
            or result.weather_code in rain_codes
        )
        if any(term in project for term in ("染发", "烫发")):
            if rain_risk:
                return "染发或烫发后建议避免淋雨，出行可携带雨具，注意保护刚完成的染烫效果。"
            return "请按门店建议做好染烫后护理，注意保护刚完成的染烫效果。"
        if any(term in project for term in ("造型", "盘发")):
            if rain_risk or (result.humidity or 0) >= 75:
                return "如有降雨或湿度较大，请注意保护刚完成的发型。"
            return "请预留出行时间，避免挤压刚完成的发型。"
        if "头皮" in project:
            tips = []
            if (result.temperature or 0) >= 30:
                tips.append("高温时注意头皮防晒")
            if rain_risk:
                tips.append("降雨时保持头皮清洁干燥")
            return "；".join(tips) + "。" if tips else "请按预约时间安排到店。"
        if rain_risk:
            return "出行可携带雨具，并预留充足到店时间。"
        return "请按预约时间安排到店。"

    @staticmethod
    def _extract_agent_output(result: Any) -> str:
        """从 LangChain 1.x agent graph 返回值中提取最后一条文本消息。"""
        if isinstance(result, dict):
            output = result.get("output")
            if isinstance(output, str) and output.strip():
                return output.strip()

            messages = result.get("messages") or []
            for message in reversed(messages):
                if isinstance(message, dict):
                    content = message.get("content")
                else:
                    content = getattr(message, "content", None)
                text = AppointmentProcessor._content_to_text(content)
                if text:
                    return text
        return ""

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
            return "\n".join(parts).strip()
        return ""
    
    async def handle_incomplete_info(
        self,
        data: Dict[str, Any],
        appointment_history: Dict[str, Any],
        session_id: str = "unknown",
        current_state: Any = None,
    ) -> AsyncGenerator[str, None]:
        """处理信息不完整的情况"""
        missing = self.get_missing_fields(appointment_history)
        if not missing:
            logger.warning(
                "appointment_state_inconsistent session_id=%s current_state=%s "
                "awaiting_confirmation=%s required_fields_complete=true",
                session_id,
                getattr(current_state, "value", current_state),
                bool(appointment_history.get("awaiting_confirmation")),
            )
            if appointment_history.get("awaiting_confirmation"):
                yield "[REPLY][预约机器人]请明确回复“是”或“不”，或者直接告诉我新的预约时间和服务。"
            else:
                yield "[REPLY][预约机器人]预约状态已恢复，请重新发送本次预约需求。"
            return

        labels = {
            "start_time": "预约日期和时间",
            "project": "服务项目",
            "duration": "服务时长",
        }
        reply = self.message_builder.create_missing_info_questions(missing)
        missing_text = "、".join(labels[field] for field in missing)
        logger.info(
            "appointment_missing_fields session_id=%s current_state=%s "
            "awaiting_confirmation=%s required_fields_complete=false missing=%s",
            session_id,
            getattr(current_state, "value", current_state),
            bool(appointment_history.get("awaiting_confirmation")),
            ",".join(missing),
        )
        yield f"[THOUGHT][预约机器人]预约信息还需要补充：{missing_text}。"
        yield f"[REPLY][预约机器人]{reply}"

"""
预约处理器

负责协调整个预约流程
"""

import os
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, AsyncGenerator, Optional
from .input_parser import InputParser
from .stylist_finder import StylistFinder
from .message_builder import MessageBuilder
from .appointment_database import AppointmentDatabase
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr
from services.service_catalog import normalize_service

logger = logging.getLogger(__name__)


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


@dataclass
class WeatherContextResult:
    """Result from the optional external weather context lookup."""

    status: str
    context: str = ""
    reason: str = ""
    http_status: Optional[int] = None


class WeatherTool(BaseTool):
    """Optional external weather context tool for post-booking travel reminders."""

    name: str = "get_current_weather"
    description: str = "获取配置位置的当前天气上下文，用于预约成功后的可选出行提醒"
    _api_key: Optional[str] = PrivateAttr(default=None)
    _enabled: bool = PrivateAttr(default=False)
    _location: str = PrivateAttr(default="")
    _timeout_seconds: float = PrivateAttr(default=3.0)
    _base_url: str = PrivateAttr(default="https://api.openweathermap.org/data/2.5/weather")
    
    def __init__(self):
        super().__init__()
        self._enabled = _env_bool("WEATHER_ENABLED", False)
        self._api_key = os.getenv("OPENWEATHER_API_KEY") or None
        self._location = (os.getenv("WEATHER_LOCATION") or "").strip()
        self._timeout_seconds = _env_float("WEATHER_TIMEOUT_SECONDS", 3.0)

    @property
    def is_configured(self) -> bool:
        return bool(self._enabled and self._api_key and self._location)

    def omission_reason(self, location: Optional[str] = None) -> Optional[str]:
        target_location = (location or self._location or "").strip()
        if not self._enabled:
            return "disabled"
        if not target_location:
            return "missing_location"
        if not self._api_key:
            return "missing_api_key"
        return None
    
    async def get_weather_context(self, location: Optional[str] = None) -> WeatherContextResult:
        """Fetch real weather context when explicitly configured.

        No synthetic weather is returned. Missing config, timeout, network errors,
        and non-200 responses all produce an omitted/unavailable status.
        """
        target_location = (location or self._location or "").strip()
        reason = self.omission_reason(target_location)
        if reason:
            logger.info("weather_context_omitted reason=%s", reason)
            return WeatherContextResult(status="omitted", reason=reason)
        
        try:
            import aiohttp

            params = {
                "q": target_location,
                "appid": self._api_key,
                "units": "metric",
                "lang": "zh_cn"
            }
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._base_url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        context = self._format_weather_context(target_location, data)
                        if context:
                            logger.info("weather_context_available location=%s", target_location)
                            return WeatherContextResult(status="available", context=context)
                        logger.warning("weather_context_unavailable reason=malformed_response")
                        return WeatherContextResult(status="unavailable", reason="malformed_response")

                    logger.warning(
                        "weather_context_unavailable reason=http_status status=%s",
                        response.status,
                    )
                    return WeatherContextResult(
                        status="unavailable",
                        reason=f"http_{response.status}",
                        http_status=response.status,
                    )
        except asyncio.TimeoutError:
            logger.warning("weather_context_unavailable reason=timeout")
            return WeatherContextResult(status="unavailable", reason="timeout")
        except Exception as exc:
            logger.warning("weather_context_unavailable reason=%s", type(exc).__name__)
            return WeatherContextResult(status="unavailable", reason=type(exc).__name__)

    @staticmethod
    def _format_weather_context(location: str, data: Dict[str, Any]) -> str:
        try:
            main = data.get("main") or {}
            weather_items = data.get("weather") or []
            description = str((weather_items[0] or {}).get("description") or "").strip()
            temp = main.get("temp")
            feels_like = main.get("feels_like")
            humidity = main.get("humidity")
            wind_speed = (data.get("wind") or {}).get("speed", 0)
            if not description or temp is None or feels_like is None or humidity is None:
                return ""
            return (
                f"{location}当前天气：{description}，"
                f"气温{_format_number(temp)}°C（体感{_format_number(feels_like)}°C），"
                f"湿度{_format_number(humidity)}%，风速{_format_number(wind_speed)}m/s。"
            )
        except (TypeError, KeyError, IndexError):
            return ""
    
    def _run(self, location: Optional[str] = None) -> str:
        """Synchronous wrapper used by LangChain Tool interfaces."""
        return asyncio.run(self.get_weather_context(location)).context
    
    async def _arun(self, location: Optional[str] = None) -> str:
        """Async wrapper used by LangChain Tool interfaces."""
        return (await self.get_weather_context(location)).context


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
    
    def update_history_from_data(self, appointment_history: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """从解析数据更新预约历史"""
        # 检查是否在等待用户确认推荐发型师
        if appointment_history.get('awaiting_confirmation'):
            return self._handle_recommendation_response(appointment_history, data)
        
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

        required_fields = ["start_time", "project", "duration"]
        return all(
            appointment_history.get(field) and appointment_history[field] != "未知" 
            for field in required_fields
        )

    def _handle_recommendation_response(self, appointment_history: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """处理用户对推荐发型师的回应"""
        user_response = data.get('confirmation', '').lower()
        
        # 判断用户是否同意推荐
        positive_responses = ['是', '好', '可以', '同意', '确定', 'yes', 'ok', '行']
        negative_responses = ['不', '不要', '不行', '不同意', '换', 'no']
        
        is_positive = any(pos in user_response for pos in positive_responses)
        is_negative = any(neg in user_response for neg in negative_responses)
        
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
        success = self.appointment_database.save_appointment(
            stylist["id"], start_time, end_time, appointment_history, session_id
        )
        if success:
            # 更新内存中的忙碌时段
            self.appointment_database.update_memory_schedule(stylist["id"], start_time, end_time)
            base_message = self.message_builder.create_appointment_success_message(stylist, appointment_history)
            weather_reminder = await self._create_optional_weather_reminder(appointment_history)
            return f"{base_message}{weather_reminder}" if weather_reminder else base_message
        else:
            return self.message_builder.create_save_failure_message()

    async def _create_optional_weather_reminder(self, appointment_history: Dict[str, Any]) -> str:
        try:
            result = await self.weather_tool.get_weather_context()
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

        tip = self._weather_care_tip(appointment_history.get("project", ""))
        return f"温馨提示：{result.context}{tip}\n"

    @staticmethod
    def _weather_care_tip(project: str) -> str:
        if any(term in project for term in ("染发", "烫发")):
            return "染发或烫发后建议避免淋雨，出行请备好防雨用品。"
        if "造型" in project:
            return "如遇降雨请注意保护刚做好的发型。"
        if "头皮" in project:
            return "户外高温时注意头皮防晒和补水。"
        return "请根据实时天气安排到店出行。"

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
    
    async def handle_incomplete_info(self, data: Dict[str, Any], appointment_history: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """处理信息不完整的情况"""
        # 确定缺失的信息
        missing = []
        if not appointment_history.get("start_time") or appointment_history.get("start_time") == "未知":
            missing.append("start_time")
        if not appointment_history.get("project") or appointment_history.get("project") == "未知":
            missing.append("project")
        if not appointment_history.get("duration") or appointment_history.get("duration") == "未知":
            missing.append("duration")
        
        reply = self.message_builder.create_missing_info_questions(missing)
        yield f"[THOUGHT][预约机器人]用户的预约信息不完整，缺少：{', '.join(missing)}，我需要询问用户补充这些信息"
        yield f"[REPLY][预约机器人]{reply}"

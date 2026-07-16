from dotenv import load_dotenv
import uuid
from langchain_core.chat_history import InMemoryChatMessageHistory
from config.model_provider import create_chat_model
from .appointment.appointment_database import AppointmentDatabase
from .appointment.availability_parser import (
    CONSULTATION,
    CREATE_BOOKING,
    SEARCH_AVAILABILITY,
    detect_message_intent,
    parse_availability_request,
)
from .appointment.appointment_processor import AppointmentProcessor
from .appointment.input_parser import InputParser
from .appointment.lifecycle_parser import LIFECYCLE_INTENTS, detect_lifecycle_intent
from .appointment.lifecycle_processor import AppointmentLifecycleProcessor
from .appointment.message_builder import MessageBuilder
from .appointment.stylist_finder import StylistFinder

load_dotenv()


class AppointmentAgent:
    """
    预约机器人主控制器
    
    职责：
    1. 初始化各个组件
    2. 管理会话状态
    3. 协调整个预约流程
    """
    
    def __init__(self, session_id=None, unrelated_callback=None):
        # 基础设置
        self.session_id = session_id or str(uuid.uuid4())
        self.unrelated_callback = unrelated_callback
        self.state = None
        
        # 初始化LLM
        self.llm = self._initialize_llm()
        
        # 初始化组件
        self.input_parser = InputParser(self.llm)
        self.stylist_finder = StylistFinder()
        self.message_builder = MessageBuilder()
        self.appointment_database = AppointmentDatabase()
        self.appointment_processor = AppointmentProcessor(
            self.input_parser, 
            self.stylist_finder,
            self.message_builder, 
            self.appointment_database,
            self.llm
        )
        self.lifecycle_processor = AppointmentLifecycleProcessor(
            self.appointment_database.appointment_service
        )
        
        # 会话管理
        self.chats_by_session_id = {}
        self.chat_history = self._get_chat_history(self.session_id)
        
        # 预约状态
        self.reset()

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)

    def _get_chat_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """获取或创建会话历史记录"""
        chat_history = self.chats_by_session_id.get(session_id)
        if chat_history is None:
            chat_history = InMemoryChatMessageHistory()
            self.chats_by_session_id[session_id] = chat_history
        return chat_history
    
    def reset(self):
        """重置预约历史和状态"""
        self._reset_business_history()
        self.chat_history.clear()

    def _reset_business_history(self):
        """Clear booking workflow state without erasing the visible conversation."""
        self.appointment_history = {
            "gender": None,
            "start_time": None,
            "duration": None,
            "project": None,
            "preference": None,
            "style_preference": None,
            "budget": None,
            "stylist_name": None,
        }
        self.finished = False

    def set_shared_state(self, shared_state):
        """设置共享状态"""
        self.state = shared_state

    def _get_lifecycle_processor(self) -> AppointmentLifecycleProcessor:
        processor = getattr(self, "lifecycle_processor", None)
        if processor is None:
            processor = AppointmentLifecycleProcessor(
                self.appointment_database.appointment_service
            )
            self.lifecycle_processor = processor
        return processor

    async def run_stream(self, user_input=None):
        """
        流式处理用户预约请求的主函数
        
        这是整个预约流程的入口点，协调各个组件完成预约
        """
        if user_input is None:
            user_input = input("用户：")
        
        try:
            detected_intent = detect_message_intent(user_input)
            lifecycle_intent = detect_lifecycle_intent(user_input)
            lifecycle_processor = getattr(self, "lifecycle_processor", None)
            lifecycle_active = AppointmentLifecycleProcessor.is_active(
                self.appointment_history
            )
            creation_confirmation_active = any(
                self.appointment_history.get(key)
                for key in (
                    "awaiting_confirmation",
                    "awaiting_slot_selection",
                    "awaiting_slot_confirmation",
                )
            )
            if lifecycle_active or (
                lifecycle_intent in LIFECYCLE_INTENTS
                and not creation_confirmation_active
            ):
                lifecycle_processor = lifecycle_processor or self._get_lifecycle_processor()
                if not lifecycle_active:
                    self._reset_business_history()
                lifecycle_result = lifecycle_processor.handle(
                    user_input,
                    self.appointment_history,
                    # A chat session is the lifecycle owner marker, not an authenticated user ID.
                    self.session_id,
                    intent=lifecycle_intent,
                )
                yield f"[REPLY][预约机器人]{lifecycle_result.message}"
                if lifecycle_result.complete:
                    self._reset_state_after_lifecycle()
                return

            if self.appointment_history.get("awaiting_slot_confirmation"):
                if detected_intent in {CREATE_BOOKING, SEARCH_AVAILABILITY}:
                    self.appointment_processor.clear_pending_availability(self.appointment_history)
                else:
                    async for token in self.appointment_processor.handle_availability_confirmation(
                        user_input, self.appointment_history, self.session_id
                    ):
                        yield token
                    if self.appointment_history.get("availability_flow_complete"):
                        self._reset_state_after_appointment()
                    return

            if self.appointment_history.get("awaiting_slot_selection"):
                if detected_intent in {CREATE_BOOKING, SEARCH_AVAILABILITY}:
                    self.appointment_processor.clear_pending_availability(self.appointment_history)
                else:
                    async for token in self.appointment_processor.handle_availability_selection(
                        user_input, self.appointment_history, self.session_id
                    ):
                        yield token
                    if self.appointment_history.get("availability_flow_complete"):
                        self._reset_state_after_appointment()
                    return

            if (
                detected_intent == CREATE_BOOKING
                and self.appointment_history.get("availability_search_active")
            ):
                self.appointment_processor.clear_pending_availability(self.appointment_history)

            if detected_intent == CONSULTATION and self.appointment_history.get("availability_search_active"):
                normalized = str(user_input or "").strip().rstrip("，。！？,.!?")
                if normalized in {"取消", "不用了", "都不合适"}:
                    self.appointment_processor.clear_pending_availability(self.appointment_history)
                    self._reset_state_after_appointment()
                    yield "[REPLY][预约机器人]已取消本次可用性查询。"
                    return

            if detected_intent == SEARCH_AVAILABILITY or self.appointment_history.get("availability_search_active"):
                stylist_names = [
                    item["name"]
                    for item in self.appointment_database.appointment_service.get_all_stylists()
                ]
                parsed = parse_availability_request(user_input, stylist_names=stylist_names)
                async for token in self.appointment_processor.handle_availability_search(
                    parsed, self.appointment_history, self.session_id
                ):
                    yield token
                return

            # Pending confirmation is deterministic and should not depend on LLM slot backfilling.
            if (
                self.appointment_history.get("awaiting_confirmation")
                and self.appointment_processor.is_explicit_confirmation_text(user_input)
            ):
                data = {"confirmation": user_input, "unrelated": False}
            else:
                # 1. 解析用户输入（内部 JSON，不向用户流式输出，避免英文字段名暴露在聊天界面）
                ai_content = ""
                for token in self.input_parser.parse_stream(user_input, self.chat_history):
                    ai_content += token
                data = self.input_parser.parse_data(ai_content)

            # 2. 将本轮槽位合并到会话状态
            self.finished = self.appointment_processor.update_history_from_data(
                self.appointment_history,
                data,
                raw_user_text=user_input,
            )

            if self.appointment_processor.should_search_availability(self.appointment_history):
                parsed = self.appointment_processor.availability_from_booking_history(
                    self.appointment_history
                )
                async for token in self.appointment_processor.handle_availability_search(
                    parsed, self.appointment_history, self.session_id
                ):
                    yield token
                return
            
            # 3. 处理与预约无关的请求
            # 如果正在等待用户确认推荐发型师，不要转交给归类机器人
            if data.get("unrelated", False) and not self.appointment_history.get('awaiting_confirmation'):
                # 注意：这里不清空预约历史，保留用户已输入的信息
                # 只设置状态为CLASSIFY，让系统转交给其他机器人处理
                if self.state:
                    from config.constants import StateEnum
                    self.state.value = StateEnum.CLASSIFY
                
                async for token in self.appointment_processor.handle_unrelated_request(
                    user_input, self.unrelated_callback, self.state
                ):
                    yield token
                return
            
            # 4. 处理预约完成的情况
            if self.finished:
                recommendation_pending = False
                booking_incomplete = False
                async for token in self.appointment_processor.handle_complete_appointment(
                    self.appointment_history, self.session_id
                ):
                    # 检查是否有推荐等待确认
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        # 将 finished 设为 False，让预约流程继续
                        self.finished = False
                        continue
                    if token == "[SIGNAL]booking_incomplete":
                        booking_incomplete = True
                        self.finished = False
                        continue
                    yield token
                
                # 只有在真正完成预约时才重置状态
                if (
                    not recommendation_pending
                    and not booking_incomplete
                    and not self.appointment_history.get('awaiting_confirmation')
                ):
                    self._reset_state_after_appointment()
                return
            
            # 5. 处理信息不完整的情况
            if self.appointment_processor.has_required_fields(self.appointment_history):
                async for token in self.appointment_processor.handle_complete_appointment(
                    self.appointment_history,
                    self.session_id,
                ):
                    yield token
                return

            async for token in self.appointment_processor.handle_incomplete_info(
                data,
                self.appointment_history,
                session_id=self.session_id,
                current_state=getattr(self.state, "value", self.state),
            ):
                yield token
                
        except Exception as e:
            yield self.message_builder.create_parse_error_message()

    async def run(self, user_input=None):
        """Non-streaming wrapper used by the task router."""
        tokens = []
        async for token in self.run_stream(user_input):
            tokens.append(token)
        return "".join(tokens)

    def _reset_state_after_appointment(self):
        """预约完成后重置状态"""
        self.reset()
        if self.state:
            from config.constants import StateEnum
            self.state.value = StateEnum.CLASSIFY

    def _reset_state_after_lifecycle(self):
        """Return routing to classification while preserving chat transcript."""
        self._reset_business_history()
        if self.state:
            from config.constants import StateEnum
            self.state.value = StateEnum.CLASSIFY

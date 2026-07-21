from dotenv import load_dotenv
from config.model_provider import create_chat_model
from config.constants import SharedState, StateEnum
from .task_classification import (
    TaskClassifier,
    StateManager,
    AgentRouter,
    UnrelatedHandler,
    ClassificationProcessor
)

load_dotenv()


class TaskClassificationAgent:
    """
    任务分类代理主控制器
    
    职责：
    1. 初始化各个分类组件
    2. 提供统一的任务分类接口
    3. 管理与其他Agent的协调
    """
    
    def __init__(self, appointment_agent, consultant_agent):
        # 基础设置
        self.appointment_agent = appointment_agent
        self.consultant_agent = consultant_agent
        
        # 初始化LLM
        self.llm = self._initialize_llm()
        
        # 初始化组件
        self.state_manager = StateManager(SharedState())
        self.task_classifier = TaskClassifier(self.llm)
        self.agent_router = AgentRouter(
            appointment_agent, 
            consultant_agent, 
            self.state_manager
        )
        self.unrelated_handler = UnrelatedHandler(self.state_manager)
        self.classification_processor = ClassificationProcessor(
            self.task_classifier,
            self.state_manager,
            self.agent_router,
            self.unrelated_handler
        )
        
        # 设置回调函数
        self._setup_callbacks()
        
        # 供外部页面读取当前对话状态
        self.state = self.state_manager.state

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)
    
    def _setup_callbacks(self):
        """设置Agent的回调函数"""
        if self.appointment_agent and hasattr(self.appointment_agent, 'unrelated_callback'):
            self.appointment_agent.unrelated_callback = self.handle_unrelated
        
        if self.consultant_agent and hasattr(self.consultant_agent, 'set_unrelated_callback'):
            self.consultant_agent.set_unrelated_callback(self.handle_unrelated_async)

    # ===========================================
    # 主要接口方法
    # ===========================================
    
    async def classify_task(self, task, owner_id=None):
        """分类任务"""
        return await self.classification_processor.process_task_sync(
            task,
            owner_id=owner_id,
        )

    async def classify_task_stream(self, task, owner_id=None):
        """流式分类任务（主要入口）"""
        async for token in self.classification_processor.process_task_stream(
            task,
            owner_id=owner_id,
        ):
            yield token

    async def route_task_stream(self, task, route, owner_id=None):
        """Route a message selected by the stateless backend pre-router."""
        if route == "appointment":
            if self.state_manager.is_in_appointment_flow():
                stream = self.agent_router.route_by_state(task, owner_id=owner_id)
            else:
                stream = self.agent_router.route_to_appointment(task, owner_id=owner_id)
        elif route == "consultation":
            stream = self.agent_router.route_to_consultation(
                task,
                owner_id=owner_id,
            )
        else:
            stream = self.classification_processor.process_task_stream(
                task,
                owner_id=owner_id,
            )
        async for token in stream:
            yield token

    async def handle_unrelated(self, user_input):
        """处理无关请求（同步版本）"""
        # 与预约无关的请求应该重新进行分类，而不是直接拒绝
        print(f"[DEBUG] 预约机器人转交的请求：{user_input}")
        
        # 重新进行任务分类
        result = ""
        async for token in self.classification_processor.process_task_stream(user_input):
            result += token
        return result

    async def handle_unrelated_async(self, user_input):
        """处理无关请求（异步流版本）"""
        # 与预约无关的请求应该重新进行分类，而不是直接拒绝
        print(f"[DEBUG] 预约机器人转交的请求：{user_input}")
        
        # 重新进行任务分类
        async for token in self.classification_processor.process_task_stream(user_input):
            yield token

    # ===========================================
    # 扩展功能方法
    # ===========================================
    
    def get_classification_info(self):
        """获取分类系统信息"""
        return self.classification_processor.get_current_state_info()
    
    def reset_conversation(self):
        """重置对话状态"""
        self.classification_processor.reset_conversation()
    
    def set_business_context(self, service_name: str = "理发店服务"):
        """设置业务上下文"""
        self.unrelated_handler.set_business_context(service_name)

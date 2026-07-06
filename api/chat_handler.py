from agents.task_classification_agent import TaskClassificationAgent
from agents.appointment_agent import AppointmentAgent
from agents.consultant_agent import ConsultantAgent
import uuid

# 全局session_id用于单用户场景
global_session_id = str(uuid.uuid4())
task_agent = None


def get_task_agent():
    global task_agent
    if task_agent is None:
        task_agent = TaskClassificationAgent(
            AppointmentAgent(session_id=global_session_id),
            ConsultantAgent(session_id=global_session_id),
        )
    return task_agent

async def ProcessUserInput_stream(user_input, state=None, context=None):
    """
    user_input: 用户输入
    state: 当前对话状态（如 None, 'classify', 'appointment', 'query', ...）
    context: 可选，保存多轮对话上下文（如 dict，可存储 agent 的 history 等）
    返回: (reply, next_state, next_context)
    """
    # 初始化 context
    if context is None:
        context = {}

    try:
        agent = get_task_agent()
        async for token in agent.classify_task_stream(user_input):
            yield token
    except Exception as exc:
        yield f"[ERROR]聊天模型未正确配置或调用失败：{exc}\n"

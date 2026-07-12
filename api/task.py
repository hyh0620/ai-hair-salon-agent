"""
简化的任务分类API

只保留第一版核心功能
"""
from fastapi import APIRouter, HTTPException
from .core.response_models import (
    TaskClassificationRequest,
    TaskClassificationResponse,
    DataResponse
)

router = APIRouter(prefix="/api/task", tags=["任务分类"])


@router.post(
    "/classify",
    response_model=DataResponse,
    summary="识别并路由用户任务",
    description="识别预约或咨询意图并调用对应 Agent；最终预约仍由结构化服务目录、排班和 SQLite 规则决定。",
)
async def classify_task(request: TaskClassificationRequest):
    """分类任务"""
    try:
        # 简化实现 - 直接导入需要的agent
        from agents.task_classification_agent import TaskClassificationAgent
        from agents.appointment_agent import AppointmentAgent
        from agents.consultant_agent import ConsultantAgent

        agent = TaskClassificationAgent(AppointmentAgent(), ConsultantAgent())
        result = await agent.classify_task(request.text)
        
        return DataResponse(
            message="任务分类成功",
            data=result
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

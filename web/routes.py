"""
Web界面路由

处理前端页面渲染和聊天功能
"""
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from api.chat_handler import (
    ProcessUserInput_stream,
    get_chat_session_registry,
    route_user_message,
)
import logging

# 创建logger实例
logger = logging.getLogger(__name__)
# 模板配置
templates = Jinja2Templates(directory="web/templates")

# Web路由器
router = APIRouter(tags=["Web界面"])

class ChatRequest(BaseModel):
    message: str
    state: str | None = None
    session_id: str | None = None
    route: str | None = None


class ChatResetRequest(BaseModel):
    session_id: str | None = None


class ChatRouteRequest(BaseModel):
    message: str

@router.get("/", response_class=HTMLResponse, summary="主页")
async def read_root(request: Request):
    """渲染主页聊天界面"""
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/status", response_class=HTMLResponse, summary="系统状态页面")
async def system_status_page(request: Request):
    from api.health import build_health_status
    from config.time_config import time_config

    return templates.TemplateResponse(
        "system_status.html",
        {
            "request": request,
            "status": build_health_status(request),
            "updated_at": time_config.current_datetime_str(),
            "version": "1.0.0",
        },
    )

@router.post("/chat/stream", summary="流式聊天")
async def chat_stream_endpoint(chat: ChatRequest):
    """处理流式聊天请求"""
    async def token_generator():
        async for token in ProcessUserInput_stream(
            chat.message,
            session_id=chat.session_id,
            route=chat.route,
        ):
            yield token
    return StreamingResponse(token_generator(), media_type="text/plain")

@router.post("/chat", summary="聊天接口")
async def chat_endpoint(chat: ChatRequest):
    """非流式路径入口，页面默认使用/chat/stream"""
    async def token_generator():
        async for token in ProcessUserInput_stream(
            chat.message,
            session_id=chat.session_id,
            route=chat.route,
        ):
            yield token
    return StreamingResponse(token_generator(), media_type="text/plain")


@router.post("/api/chat/route", include_in_schema=False)
async def route_chat_message(payload: ChatRouteRequest):
    """Select the page endpoint without executing a task or mutating session state."""
    return {"route": route_user_message(payload.message)}


@router.post("/api/chat/reset", include_in_schema=False)
async def reset_chat_session(payload: ChatResetRequest):
    new_session_id = get_chat_session_registry().reset(payload.session_id)
    return {"status": "reset", "session_id": new_session_id}

@router.get("/user_behavior", response_class=HTMLResponse, summary="用户行为分析页面")
async def user_behavior_page(request: Request):
    """用户行为分析页面"""
    return templates.TemplateResponse("user_behavior_analysis.html", {"request": request})

@router.get("/knowledge", response_class=HTMLResponse, summary="知识服务状态页面")
async def knowledge_page(request: Request):
    """MCP RAG 知识服务状态页面"""
    try:
        from api.knowledge import get_knowledge_status

        knowledge_data = await get_knowledge_status()
        return templates.TemplateResponse("knowledge_management.html", {
            "request": request,
            "knowledge_status": knowledge_data,
        })
    except Exception as e:
        return templates.TemplateResponse("knowledge_management.html", {
            "request": request,
            "knowledge_status": None,
            "error": str(e)
        })

@router.get("/stylists", response_class=HTMLResponse, summary="发型师状态页面")
async def stylist_page(request: Request):
    """发型师状态页面"""
    # 通过API层获取发型师数据
    try:
        from api.stylist import get_all_stylists
        
        # 调用API层函数获取数据
        stylists = await get_all_stylists()
        
        return templates.TemplateResponse("stylist.html", {
            "request": request,
            "stylists": stylists
        })
    except Exception as e:
        return templates.TemplateResponse("stylist.html", {
            "request": request,
            "stylists": [],
            "error": str(e)
        })

@router.get("/stylist-schedule", response_class=HTMLResponse, summary="发型师排班页面")
async def stylist_schedule_page(
    request: Request,
    selected_date: date | None = Query(default=None, alias="date"),
):
    """发型师排班页面"""
    try:
        from api.stylist import build_stylist_schedules
        from config.time_config import time_config
        target_date = selected_date or time_config.today().date()
        schedules_data = build_stylist_schedules(target_date)
        schedule = [
            {
                "id": item.stylist_id,
                "name": item.stylist_name,
                "busy_periods": [period.model_dump() for period in item.busy_periods],
            }
            for item in schedules_data.stylists
        ]
        
        return templates.TemplateResponse("stylist_schedule.html", {
            "request": request,
            "schedule": schedule,
            "selected_date": target_date.isoformat(),
            "previous_date": (target_date - timedelta(days=1)).isoformat(),
            "next_date": (target_date + timedelta(days=1)).isoformat(),
            "today": time_config.today().date().isoformat(),
        })
    except Exception as e:
        logger.error(f"加载发型师排班数据失败: {str(e)}")
        fallback_date = selected_date or date.today()
        return templates.TemplateResponse("stylist_schedule.html", {
            "request": request,
            "schedule": [],
            "error": str(e),
            "selected_date": fallback_date.isoformat(),
            "previous_date": (fallback_date - timedelta(days=1)).isoformat(),
            "next_date": (fallback_date + timedelta(days=1)).isoformat(),
            "today": date.today().isoformat(),
        })

@router.get("/user_behavior_analysis", response_class=HTMLResponse, summary="用户行为分析页面")
async def user_behavior_analysis_page(request: Request):
    """用户行为分析页面"""
    return templates.TemplateResponse("user_behavior_analysis.html", {"request": request})

@router.get("/admin", response_class=HTMLResponse, summary="系统管理页面")
async def admin_dashboard(request: Request):
    """系统管理仪表板"""
    try:
        # 通过API层获取系统状态信息
        from api.stylist import get_all_stylists
        
        # 获取发型师数据
        stylists = await get_all_stylists()
        
        # 数据库信息
        db_info = {
            "knowledge_backend": "外部 MCP Knowledge Service",
            "rag_collection": "salon_knowledge",
            "stylists_count": len(stylists),
        }
        
        return templates.TemplateResponse("admin_dashboard.html", {
            "request": request,
            "db_info": db_info,
            "stylists": stylists[:5]  # 只显示前5个发型师
        })
    except Exception as e:
        return templates.TemplateResponse("admin_dashboard.html", {
            "request": request,
            "db_info": {},
            "stylists": [],
            "error": str(e)
        })

@router.get("/admin/database", response_class=HTMLResponse, summary="数据库管理页面")
async def database_admin_page(request: Request):
    """数据库管理页面"""
    try:
        # 通过API层获取数据库统计信息
        from api.stylist import get_all_stylists
        
        # 获取发型师数据
        stylists = await get_all_stylists()
        
        stats = {
            "knowledge_backend": "外部 MCP Knowledge Service",
            "rag_collection": "salon_knowledge",
            "stylists": len(stylists),
            "appointments": 0  # TODO: 通过API获取预约数量
        }
        
        return templates.TemplateResponse("database_admin.html", {
            "request": request,
            "stats": stats
        })
    except Exception as e:
        return templates.TemplateResponse("database_admin.html", {
            "request": request,
            "stats": {},
            "error": str(e)
        })

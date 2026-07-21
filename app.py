"""
FastAPI应用程序

主应用程序入口，配置中间件、路由和异常处理
自动初始化理发店发型师数据并连接外部 RAG MCP 服务
"""
from contextlib import asynccontextmanager
import os
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from services.stylist_service import StylistService
from services.recommendation_service import RecommendationService
from services.mcp_knowledge_gateway import get_mcp_knowledge_gateway
from services.auth_rate_limit_service import AuthRateLimiter
from config.auth_rate_limit_config import AuthRateLimitConfig
from config.trace_context import new_trace_id, reset_trace_id, set_trace_id
import logging

# 导入路由
from api import api_routers
from api.core.exceptions import api_exception_handler, general_exception_handler, BusinessException
from web import router as web_router

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "系统状态", "description": "应用、数据库和外部知识服务健康状态。"},
    {"name": "账户认证", "description": "可选本地账户、Argon2 密码与 JWT 身份。"},
    {"name": "预约管理", "description": "由确定性服务目录、营业时间、排班和冲突规则执行预约。"},
    {"name": "咨询服务", "description": "通过 MCP Knowledge Service 检索知识并返回 citations。"},
    {"name": "任务分类", "description": "识别用户意图并在预约与咨询职责之间路由。"},
    {"name": "知识服务状态", "description": "查看 MCP Knowledge Gateway 的连接状态。"},
    {"name": "发型师管理", "description": "查询发型师及 SQLite 持久化排班。"},
    {"name": "用户行为分析", "description": "查询历史服务偏好和回访提醒。"},
]


def _get_cors_allowed_origins() -> list[str]:
    raw_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

async def initialize_system(app: FastAPI):
    """系统启动时自动初始化"""
    try:
        logger.info("🚀 正在初始化理发店智能预约系统...")
        
        # 初始化发型师服务
        logger.info("✂️ 初始化发型师服务...")
        stylist_service = StylistService()
        stylist_service.initialize_default_stylists()

        logger.info("📚 启动 MCP Knowledge Service 知识网关...")
        rag_gateway = get_mcp_knowledge_gateway()
        app.state.rag_gateway = rag_gateway
        try:
            await rag_gateway.start()
            if rag_gateway.is_connected:
                logger.info("✅ MCP 知识网关启动成功")
            else:
                logger.warning("⚠️ MCP 知识网关未启用")
        except Exception as exc:
            logger.error("❌ MCP 知识网关启动失败，咨询接口将返回 503: %s", exc)
        
        # 初始化推荐服务
        logger.info("🎯 启动推荐调度服务...")
        recommendation_service = RecommendationService()
        if recommendation_service.start_scheduler():
            logger.info("✅ 推荐调度服务启动成功")
        else:
            logger.warning("⚠️ 推荐调度服务启动失败")
        
        logger.info("✅ 系统初始化完成！")
        
    except Exception as e:
        logger.error(f"❌ 系统初始化失败: {e}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialize_system(app)
    try:
        yield
    finally:
        rag_gateway = getattr(app.state, "rag_gateway", None)
        if rag_gateway:
            await rag_gateway.stop()

def create_app() -> FastAPI:
    """创建FastAPI应用实例"""
    
    app = FastAPI(
        title="理发店智能预约AI代理",
        description="提供理发店预约管理、智能咨询、发型师排班和用户行为分析等功能的API服务",
        version="1.0.0",
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.state.auth_rate_limiter = AuthRateLimiter(AuthRateLimitConfig.from_env())

    cors_allowed_origins = _get_cors_allowed_origins()
    if cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def trace_middleware(request, call_next):
        trace_id = request.headers.get("x-trace-id") or request.headers.get("X-Trace-ID") or new_trace_id()
        token = set_trace_id(trace_id)
        request.state.trace_id = trace_id
        start = time.perf_counter()
        logger.info("request_start trace_id=%s method=%s path=%s", trace_id, request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_error trace_id=%s method=%s path=%s", trace_id, request.method, request.url.path)
            raise
        finally:
            reset_trace_id(token)

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Trace-ID"] = trace_id
        logger.info(
            "request_end trace_id=%s method=%s path=%s status=%s duration_ms=%s",
            trace_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    # 注册异常处理器
    app.add_exception_handler(BusinessException, api_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)

    # 注册API路由
    for router in api_routers:
        app.include_router(router)

    # 注册Web界面路由
    app.include_router(web_router, include_in_schema=False)

    # 静态文件
    app.mount("/static", StaticFiles(directory="web/static"), name="static")

    return app

# 创建应用实例
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, proxy_headers=False)

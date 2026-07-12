"""Operational health checks."""

from __future__ import annotations

import os
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import APIRouter, Request
from sqlalchemy import text

from db.base import SessionManager
from services.mcp_knowledge_gateway import get_mcp_knowledge_gateway

router = APIRouter(tags=["系统状态"])


@router.get(
    "/health",
    summary="获取系统健康状态",
    description="返回应用、SQLite、MCP RAG collection 和 LLM 配置状态的机器可读结果。",
)
async def get_health(request: Request) -> Dict[str, Any]:
    return build_health_status(request)


def build_health_status(request: Request) -> Dict[str, Any]:
    """Build health data without creating another MCP gateway or self-HTTP call."""
    gateway = getattr(request.app.state, "rag_gateway", None) or get_mcp_knowledge_gateway()
    database_status = _database_status()
    mcp_status = "healthy" if gateway.enabled and gateway.is_connected else "unavailable"
    weather_status, weather_provider, weather_location = _weather_status()
    return {
        "app": "healthy",
        "database": database_status,
        "mcp_rag": mcp_status,
        "rag_collection": gateway.collection if mcp_status == "healthy" else "unavailable",
        "llm": "configured" if _llm_configured() else "not_configured",
        "weather": weather_status,
        "weather_provider": weather_provider,
        "weather_location": weather_location,
    }


def _database_status() -> str:
    manager = None
    try:
        manager = SessionManager()
        with manager.engine.connect() as conn:
            conn.execute(text("select 1"))
        return "healthy"
    except Exception:
        return "unavailable"
    finally:
        if manager:
            manager.close()


def _llm_configured() -> bool:
    load_dotenv()
    provider = (os.getenv("MODEL_PROVIDER") or "").strip().lower()
    if provider == "azure":
        keys = (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_VERSION",
        )
    else:
        keys = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    return all(_usable_env(key) for key in keys)


def _weather_status() -> tuple[str, str, str]:
    from agents.appointment.appointment_processor import WeatherTool

    tool = WeatherTool()
    status = "configured" if tool.is_configured else "disabled"
    return status, tool.provider, tool.location_name


def _usable_env(key: str) -> bool:
    value = os.getenv(key, "")
    return bool(value and not value.startswith("your_") and "YOUR_" not in value)

"""Operational health checks."""

from __future__ import annotations

import os
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import APIRouter, Request
from sqlalchemy import text

from db.base import SessionManager
from services.mcp_knowledge_gateway import get_mcp_knowledge_gateway

router = APIRouter(tags=["健康检查"])


@router.get("/health")
async def get_health(request: Request) -> Dict[str, Any]:
    gateway = getattr(request.app.state, "rag_gateway", None) or get_mcp_knowledge_gateway()
    database_status = _database_status()
    mcp_status = "healthy" if gateway.enabled and gateway.is_connected else "unavailable"
    return {
        "app": "healthy",
        "database": database_status,
        "mcp_rag": mcp_status,
        "rag_collection": gateway.collection if mcp_status == "healthy" else "unavailable",
        "llm": "configured" if _llm_configured() else "not_configured",
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


def _usable_env(key: str) -> bool:
    value = os.getenv(key, "")
    return bool(value and not value.startswith("your_") and "YOUR_" not in value)

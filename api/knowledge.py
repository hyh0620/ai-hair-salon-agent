"""MCP RAG knowledge service status API."""

from fastapi import APIRouter

from services.mcp_knowledge_gateway import get_mcp_knowledge_gateway

router = APIRouter(prefix="/api/knowledge", tags=["知识服务状态"])


@router.get("/")
async def get_knowledge_status():
    gateway = get_mcp_knowledge_gateway()
    return {
        "status": "success",
        "knowledge_backend": "mcp_knowledge_service",
        "retrieval_mode": "mcp_hybrid_search",
        "message": "正式咨询知识检索由独立 MCP Knowledge Service 提供。",
        "gateway": gateway.status(),
    }


@router.post("/reconnect")
async def reconnect_knowledge_gateway():
    gateway = get_mcp_knowledge_gateway()
    await gateway.reconnect()
    return {
        "status": "success",
        "message": "MCP 知识网关已重新连接",
        "gateway": gateway.status(),
    }

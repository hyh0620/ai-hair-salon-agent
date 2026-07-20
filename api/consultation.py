"""Consultation API backed by MCP Knowledge Service."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config.trace_context import get_trace_id
from config.model_provider import create_chat_model, is_chat_model_configured
from services.mcp_knowledge_gateway import (
    KnowledgeQueryResult,
    MCPRAGUnavailable,
    MCPToolError,
    get_mcp_knowledge_gateway,
)
from services.service_catalog import all_services, normalize_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/consultation", tags=["咨询服务"])


class ConsultationQueryRequest(BaseModel):
    question: str = Field(..., min_length=1)


class ConsultationSource(BaseModel):
    title: str
    source: str
    score: float = 0.0
    page: Optional[int] = None


class ConsultationQueryResponse(BaseModel):
    trace_id: str
    answer: str
    sources: List[ConsultationSource]
    retrieval_mode: str
    collection: str
    rag_status: str
    llm_status: str
    source_count: int


@router.post(
    "/query",
    response_model=ConsultationQueryResponse,
    summary="查询理发店知识",
    description="通过 MCP Knowledge Service 执行 Dense Retrieval、BM25 与 RRF，并返回回答和 citations；不参与预约价格、时长、排班或冲突裁决。",
)
async def query_consultation(request: Request, payload: ConsultationQueryRequest):
    """Query MCP Knowledge Service and answer from retrieved salon knowledge."""
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or get_trace_id()
    gateway = getattr(request.app.state, "rag_gateway", None) or get_mcp_knowledge_gateway()
    logger.info("consultation_route trace_id=%s route=mcp_rag question_len=%s", trace_id, len(payload.question))
    try:
        retrieval = await gateway.query_knowledge(payload.question)
    except MCPRAGUnavailable as exc:
        logger.warning("consultation_mcp_unavailable trace_id=%s error=%s", trace_id, exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": exc.code,
                "message": "知识检索服务当前不可用，请稍后重试。",
                "trace_id": trace_id,
            },
        ) from exc
    except MCPToolError as exc:
        logger.warning("consultation_mcp_tool_error trace_id=%s error=%s", trace_id, exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": exc.code,
                "message": str(exc) or "知识检索工具调用失败。",
                "trace_id": trace_id,
            },
        ) from exc

    service_context = _service_catalog_context(payload.question, retrieval.content)
    llm_status = "not_configured"
    if is_chat_model_configured():
        try:
            answer = await _generate_llm_answer(payload.question, retrieval, service_context)
            llm_status = "available"
        except Exception as exc:
            logger.warning("Consultation LLM answer generation failed: %s", exc)
            answer = _fallback_answer(payload.question, retrieval, service_context)
            llm_status = "error"
    else:
        answer = _fallback_answer(payload.question, retrieval, service_context)

    sources = [ConsultationSource(**source.to_public_dict()) for source in retrieval.sources]
    logger.info(
        "consultation_response trace_id=%s rag_status=%s llm_status=%s source_count=%s",
        trace_id,
        retrieval.rag_status,
        llm_status,
        len(sources),
    )
    return ConsultationQueryResponse(
        trace_id=trace_id,
        answer=answer,
        sources=sources,
        retrieval_mode=retrieval.retrieval_mode,
        collection=retrieval.collection,
        rag_status=retrieval.rag_status,
        llm_status=llm_status,
        source_count=len(sources),
    )


@router.post(
    "/ask",
    response_model=ConsultationQueryResponse,
    summary="理发店咨询问答兼容入口",
    include_in_schema=False,
)
async def ask_consultation(request: Request, payload: ConsultationQueryRequest):
    """Compatibility alias for the formal MCP-backed consultation endpoint."""
    return await query_consultation(request, payload)


async def _generate_llm_answer(
    question: str,
    retrieval: KnowledgeQueryResult,
    service_context: str,
) -> str:
    llm = create_chat_model(temperature=0.2)
    prompt = (
        "你是理发店前台咨询助手。请只根据给定的知识库检索结果和结构化服务目录回答，"
        "不要编造价格、时长、发型师档期或预约结果。\n\n"
        "如果问题涉及价格或服务时长，必须以“结构化服务目录”为准；"
        "如果检索文本和结构化目录不一致，说明以系统服务目录为准。\n\n"
        f"用户问题：{question}\n\n"
        f"结构化服务目录：\n{service_context or '本问题不涉及结构化价格或时长。'}\n\n"
        f"MCP 检索结果：\n{retrieval.content}\n\n"
        "请用中文给出简洁、可执行的回答，必要时提到来源编号。"
    )
    response = await llm.ainvoke([{"role": "user", "content": prompt}])
    return str(response.content).strip()


def _fallback_answer(
    question: str,
    retrieval: KnowledgeQueryResult,
    service_context: str,
) -> str:
    lines = ["当前未配置回答模型，以下为知识库检索摘要："]
    if service_context:
        lines.append("")
        lines.append("结构化服务目录信息：")
        lines.extend(f"- {line}" for line in service_context.splitlines() if line.strip())
    if retrieval.sources:
        lines.append("")
        lines.append("检索到的相关内容：")
        for source in retrieval.sources[:4]:
            snippet = source.text_snippet or source.title
            lines.append(f"- [{source.title}] {snippet}")
    else:
        lines.append("")
        lines.append("未检索到明确来源，请稍后重试或换个问法。")
    return "\n".join(lines)


def _service_catalog_context(question: str, retrieved_content: str) -> str:
    price_terms = ("价格", "多少钱", "收费", "费用", "时长", "多久", "多长时间")
    matched_service = normalize_service(question)
    if matched_service:
        services = [matched_service]
    elif any(term in question for term in price_terms):
        services = all_services()
    else:
        services = []

    if services and any(term in retrieved_content for term in price_terms):
        logger.warning("RAG content mentions price/duration; service_catalog will be used as source of truth.")

    return "\n".join(
        f"{service.name}: {service.standard_price}元 / {service.standard_duration}分钟"
        for service in services
    )

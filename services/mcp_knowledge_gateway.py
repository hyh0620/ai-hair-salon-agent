"""Unified knowledge gateway backed by MCP Knowledge Service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config.trace_context import get_trace_id

logger = logging.getLogger(__name__)


class MCPRAGUnavailable(RuntimeError):
    code = "mcp_rag_unavailable"


class MCPToolError(RuntimeError):
    code = "mcp_tool_error"


@dataclass
class KnowledgeSource:
    title: str
    source: str
    score: float = 0.0
    page: Optional[int] = None
    chunk_id: Optional[str] = None
    text_snippet: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "score": self.score,
            "page": self.page,
        }


@dataclass
class KnowledgeQueryResult:
    query: str
    collection: str
    content: str
    sources: List[KnowledgeSource]
    metadata: Dict[str, Any]
    retrieval_mode: str = "mcp_hybrid_search"
    rag_status: str = "available"


class MCPKnowledgeGateway:
    """Long-lived MCP stdio client for the external MCP Knowledge Service."""

    REQUIRED_TOOL = "query_knowledge_hub"

    def __init__(
        self,
        enabled: bool,
        server_python: str,
        server_module: str,
        server_cwd: str,
        collection: str,
        top_k: int,
    ):
        self.enabled = enabled
        self.server_python = server_python
        self.server_module = server_module
        self.server_cwd = server_cwd
        self.collection = collection
        self.top_k = top_k
        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._tools: List[str] = []
        self._lock = asyncio.Lock()
        self._last_error: Optional[str] = None

    @classmethod
    def from_env(cls) -> "MCPKnowledgeGateway":
        load_dotenv()
        return cls(
            enabled=_env_bool("RAG_MCP_ENABLED", False),
            server_python=os.getenv("RAG_MCP_SERVER_PYTHON", ""),
            server_module=os.getenv("RAG_MCP_SERVER_MODULE", "src.mcp_server.server"),
            server_cwd=os.getenv("RAG_MCP_SERVER_CWD", ""),
            collection=os.getenv("RAG_MCP_COLLECTION", "salon_knowledge"),
            top_k=int(os.getenv("RAG_MCP_QUERY_TOP_K", "4")),
        )

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    @property
    def tools(self) -> List[str]:
        return list(self._tools)

    async def start(self) -> None:
        if not self.enabled:
            self._last_error = "RAG_MCP_ENABLED is false"
            logger.warning("MCP RAG gateway disabled by configuration")
            return
        if self._session is not None:
            return
        if not self.server_python or not self.server_cwd:
            raise MCPRAGUnavailable(
                "RAG_MCP_SERVER_PYTHON and RAG_MCP_SERVER_CWD must be configured when RAG_MCP_ENABLED=true"
            )
        if not Path(self.server_python).exists():
            raise MCPRAGUnavailable(f"MCP server python not found: {self.server_python}")
        if not Path(self.server_cwd).exists():
            raise MCPRAGUnavailable(f"MCP server cwd not found: {self.server_cwd}")

        logger.info("Starting MCP Server: %s -m %s", self.server_python, self.server_module)
        logger.info("MCP Server CWD: %s", self.server_cwd)
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self.server_python,
                args=["-m", self.server_module],
                cwd=self.server_cwd,
                env=dict(os.environ),
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            init_result = await session.initialize()
            logger.info("MCP initialize succeeded: %s", getattr(init_result, "serverInfo", None))

            tool_result = await session.list_tools()
            self._tools = [tool.name for tool in tool_result.tools]
            logger.info("MCP tools/list succeeded: %s", ", ".join(self._tools))
            if self.REQUIRED_TOOL not in self._tools:
                raise MCPRAGUnavailable(
                    f"Required MCP tool '{self.REQUIRED_TOOL}' not found. Tools: {self._tools}"
                )

            self._exit_stack = stack
            self._session = session
            self._last_error = None
        except Exception as exc:
            await stack.aclose()
            self._session = None
            self._last_error = str(exc)
            raise

    async def stop(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None
        logger.info("MCP RAG gateway stopped")

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.is_connected,
            "server_python": self.server_python,
            "server_module": self.server_module,
            "server_cwd": self.server_cwd,
            "collection": self.collection,
            "top_k": self.top_k,
            "tools": self.tools,
            "last_error": self._last_error,
        }

    async def query_knowledge(
        self,
        query: str,
        collection: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> KnowledgeQueryResult:
        if not self.enabled or self._session is None:
            raise MCPRAGUnavailable("知识检索服务当前不可用，请稍后重试。")

        effective_collection = collection or self.collection
        effective_top_k = top_k or self.top_k
        args = {
            "query": query,
            "top_k": effective_top_k,
            "collection": effective_collection,
        }
        trace_id = get_trace_id()
        logger.info(
            "mcp_query_start trace_id=%s collection=%s top_k=%s query_preview=%r",
            trace_id,
            effective_collection,
            effective_top_k,
            _safe_preview(query),
        )

        async with self._lock:
            try:
                result = await self._session.call_tool(self.REQUIRED_TOOL, args)
            except Exception as exc:
                self._last_error = str(exc)
                raise MCPRAGUnavailable("知识检索服务当前不可用，请稍后重试。") from exc

        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            text = _content_to_text(getattr(result, "content", []))
            self._last_error = text or "MCP tool returned isError"
            raise MCPToolError(self._last_error)

        parsed = self._parse_call_tool_result(result)
        source_count = len(parsed.sources)
        logger.info(
            "mcp_query_end trace_id=%s collection=%s retrieval_mode=%s source_count=%s",
            trace_id,
            effective_collection,
            parsed.retrieval_mode,
            source_count,
        )
        return KnowledgeQueryResult(
            query=query,
            collection=effective_collection,
            content=parsed.content,
            sources=parsed.sources,
            metadata=parsed.metadata,
        )

    def _parse_call_tool_result(self, result: Any) -> KnowledgeQueryResult:
        content_items = getattr(result, "content", None) or []
        text_blocks = [_content_item_text(item) for item in content_items]
        text_blocks = [text for text in text_blocks if text]
        content = "\n\n".join(_non_reference_blocks(text_blocks)).strip()

        structured = (
            getattr(result, "structuredContent", None)
            or getattr(result, "structured_content", None)
            or _extract_references_json("\n".join(text_blocks))
        )
        if not isinstance(structured, dict):
            structured = {}

        citations = structured.get("citations") or structured.get("references") or []
        metadata = structured.get("metadata") or {}
        sources = [self._source_from_citation(item) for item in citations if isinstance(item, dict)]
        return KnowledgeQueryResult(
            query=str(metadata.get("query", "")),
            collection=str(metadata.get("collection", self.collection)),
            content=content,
            sources=sources,
            metadata=metadata,
        )

    @staticmethod
    def _source_from_citation(item: Dict[str, Any]) -> KnowledgeSource:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        source = str(
            item.get("source")
            or item.get("source_path")
            or metadata.get("source_path")
            or metadata.get("source")
            or "unknown"
        )
        display_source = Path(source).name if source != "unknown" else source
        title = str(item.get("title") or metadata.get("title") or display_source or "知识来源")
        return KnowledgeSource(
            title=title,
            source=display_source,
            score=float(item.get("score", 0.0) or 0.0),
            page=_to_int_or_none(item.get("page")),
            chunk_id=item.get("chunk_id") or item.get("id"),
            text_snippet=str(item.get("text_snippet") or item.get("snippet") or item.get("text") or ""),
            metadata=metadata,
        )


_gateway: Optional[MCPKnowledgeGateway] = None


def get_mcp_knowledge_gateway() -> MCPKnowledgeGateway:
    global _gateway
    if _gateway is None:
        _gateway = MCPKnowledgeGateway.from_env()
    return _gateway


def reset_mcp_knowledge_gateway(gateway: Optional[MCPKnowledgeGateway] = None) -> None:
    global _gateway
    _gateway = gateway


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _content_to_text(content_items: List[Any]) -> str:
    parts = []
    for item in content_items:
        text = _content_item_text(item)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _content_item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("content") or "")
    return str(getattr(item, "text", "") or "")


def _non_reference_blocks(text_blocks: List[str]) -> List[str]:
    cleaned = []
    for text in text_blocks:
        without_references = re.sub(
            r"\n?---\s*\n\*\*References \(JSON\):\*\*\s*```json\s*.*?```",
            "",
            text,
            flags=re.S,
        ).strip()
        if without_references:
            cleaned.append(without_references)
    return cleaned


def _extract_references_json(text: str) -> Dict[str, Any]:
    match = re.search(r"\*\*References \(JSON\):\*\*\s*```json\s*(.*?)\s*```", text, re.S)
    if not match:
        match = re.search(r"References \(JSON\):\s*```json\s*(.*?)\s*```", text, re.S)
    if not match:
        match = re.search(r"```json\s*(.*?)\s*```", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Failed to parse References JSON from MCP response")
        return {}


def _to_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_preview(value: str, limit: int = 40) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")

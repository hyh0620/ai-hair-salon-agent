import asyncio
import json
from types import SimpleNamespace

import pytest

from api.consultation import ConsultationQueryRequest, query_consultation
from api.knowledge import get_knowledge_status
from services.mcp_knowledge_gateway import (
    KnowledgeQueryResult,
    KnowledgeSource,
    MCPKnowledgeGateway,
    MCPToolError,
)


def run(coro):
    return asyncio.run(coro)


def text_block(text):
    return SimpleNamespace(text=text)


def call_result(content, is_error=False):
    return SimpleNamespace(content=content, isError=is_error)


def references_json(citations=None, metadata=None):
    payload = {
        "citations": citations or [],
        "metadata": metadata or {"query": "染发注意事项", "collection": "salon_knowledge", "result_count": 1},
    }
    return "\n---\n**References (JSON):**\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def test_gateway_parses_successful_retrieval_with_citations():
    gateway = MCPKnowledgeGateway(
        enabled=True,
        server_python="python",
        server_module="src.mcp_server.server",
        server_cwd="/tmp",
        collection="salon_knowledge",
        top_k=4,
    )
    result = call_result([
        text_block("## 检索结果\n染后48小时内建议减少高温清洗。"),
        text_block(references_json(citations=[{
            "index": 1,
            "chunk_id": "chunk-1",
            "source": "knowledge_sources/salon_knowledge_base.pdf",
            "score": 0.93,
            "page": 2,
            "text_snippet": "染后48小时内建议减少高温清洗。",
            "metadata": {"title": "AI Hair Salon Knowledge Base", "chunk_index": 3},
        }])),
    ])

    parsed = gateway._parse_call_tool_result(result)

    assert parsed.collection == "salon_knowledge"
    assert parsed.sources[0].title == "AI Hair Salon Knowledge Base"
    assert parsed.sources[0].page == 2
    assert parsed.sources[0].score == 0.93


def test_gateway_parses_empty_result():
    gateway = MCPKnowledgeGateway(True, "python", "module", "/tmp", "salon_knowledge", 4)
    parsed = gateway._parse_call_tool_result(call_result([
        text_block("## 未找到相关结果"),
        text_block(references_json(citations=[], metadata={
            "query": "不存在的问题",
            "collection": "salon_knowledge",
            "result_count": 0,
            "isEmpty": True,
        })),
    ]))

    assert parsed.sources == []
    assert "未找到" in parsed.content


def test_gateway_raises_on_mcp_is_error():
    class FakeSession:
        async def call_tool(self, name, args):
            return call_result([text_block("boom")], is_error=True)

    gateway = MCPKnowledgeGateway(True, "python", "module", "/tmp", "salon_knowledge", 4)
    gateway._session = FakeSession()

    with pytest.raises(MCPToolError):
        run(gateway.query_knowledge("染发", top_k=1))


def test_consultation_api_returns_fields_without_llm(monkeypatch):
    class FakeGateway:
        async def query_knowledge(self, query):
            return KnowledgeQueryResult(
                query=query,
                collection="salon_knowledge",
                content="## 检索结果\n染发前建议沟通目标发色。",
                sources=[KnowledgeSource(
                    title="染发护理",
                    source="knowledge_sources/salon_knowledge_base.pdf",
                    score=0.91,
                    page=2,
                    text_snippet="染发前建议沟通目标发色。",
                )],
                metadata={"result_count": 1},
            )

    for key in ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"]:
        monkeypatch.setenv(key, "your_test_placeholder")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rag_gateway=FakeGateway())))

    response = run(query_consultation(request, ConsultationQueryRequest(question="染发前后有什么注意事项？")))

    assert response.retrieval_mode == "mcp_hybrid_search"
    assert response.rag_status == "available"
    assert response.llm_status == "not_configured"
    assert response.source_count == 1
    assert response.sources[0].title == "染发护理"


def test_knowledge_status_api(monkeypatch):
    class FakeGateway:
        def status(self):
            return {
                "enabled": True,
                "connected": True,
                "collection": "salon_knowledge",
                "tools": ["query_knowledge_hub"],
            }

    monkeypatch.setattr("api.knowledge.get_mcp_knowledge_gateway", lambda: FakeGateway())

    result = run(get_knowledge_status())

    assert result["knowledge_backend"] == "mcp_knowledge_service"
    assert result["gateway"]["collection"] == "salon_knowledge"

"""Knowledge retriever backed only by the MCP knowledge gateway."""

from typing import Any, Dict, List

from services.mcp_knowledge_gateway import get_mcp_knowledge_gateway


class KnowledgeRetriever:
    """咨询知识检索器：正式运行路径只调用 Modular RAG MCP Server。"""

    def __init__(self):
        self.gateway = get_mcp_knowledge_gateway()

    async def initialize(self):
        """Gateway lifecycle is managed by FastAPI lifespan."""
        return None

    async def search_knowledge(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        result = await self.gateway.query_knowledge(query, top_k=top_k)
        docs = []
        for source in result.sources:
            docs.append({
                "content": source.text_snippet,
                "category": source.metadata.get("section", "MCP RAG"),
                "source": source.source,
                "title": source.title,
                "score": source.score,
                "page": source.page,
                "rank": len(docs) + 1,
            })
        if not docs and result.content:
            docs.append({
                "content": result.content,
                "category": "MCP RAG",
                "source": "Modular RAG MCP Server",
                "title": "MCP 检索结果",
                "score": 0.0,
                "page": None,
                "rank": 1,
            })
        return docs

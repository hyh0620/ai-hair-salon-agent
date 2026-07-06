# RAG Service Integration

AI Hair Salon Agent uses MCP Knowledge Service as its non-structured knowledge retrieval layer.

## Required Environment Variables

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

## Startup Behavior

During FastAPI lifespan startup:

1. `MCPKnowledgeGateway` reads the environment.
2. It starts the MCP server through official stdio transport.
3. It calls `initialize`.
4. It calls `tools/list`.
5. It verifies `query_knowledge_hub` is available.

## Query Flow

```text
POST /api/consultation/query
  -> MCPKnowledgeGateway.query_knowledge
  -> ClientSession.call_tool("query_knowledge_hub")
  -> sources + citations
  -> optional chat-model answer generation
```

## Error Contract

If MCP is unavailable, consultation returns HTTP 503 with `mcp_rag_unavailable`. The appointment API remains available.

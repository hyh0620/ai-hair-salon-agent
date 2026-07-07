# RAG Service Integration / RAG 服务集成

AI Hair Salon Agent uses MCP Knowledge Service as its non-structured knowledge retrieval layer.

AI Hair Salon Agent 使用 MCP Knowledge Service 作为非结构化知识检索层。

## Environment Variables / 环境变量

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

Set `RAG_MCP_ENABLED=true` only after `RAG_MCP_SERVER_PYTHON` and `RAG_MCP_SERVER_CWD` point to a valid local MCP Knowledge Service checkout.

只有在 `RAG_MCP_SERVER_PYTHON` 和 `RAG_MCP_SERVER_CWD` 已指向有效本地 MCP Knowledge Service 后，才设为 `true`。

## FastAPI Lifespan / FastAPI 生命周期

During FastAPI lifespan startup:

1. `MCPKnowledgeGateway` reads the environment.
2. It starts MCP Knowledge Service as a child process through official stdio transport.
3. It calls `initialize`.
4. It calls `tools/list`.
5. It verifies `query_knowledge_hub` is available.
6. It reuses the MCP session for consultation requests.
7. FastAPI shutdown closes the MCP child process.

FastAPI 启动后会通过 stdio 拉起 MCP 子进程、初始化、发现工具并校验 `query_knowledge_hub`，关闭时清理该子进程。

Manual `python -m src.mcp_server.server` startup is only needed for standalone MCP verification.

手动执行 `python -m src.mcp_server.server` 只用于单独验证 MCP Server。

## Query Flow / 查询流程

```text
POST /api/consultation/query
  -> MCPKnowledgeGateway.query_knowledge
  -> ClientSession.call_tool("query_knowledge_hub")
  -> sources + citations
  -> optional chat-model answer generation
```

Consultation uses RAG for care guidance, store information, booking policy, and membership rules.

咨询路径用 RAG 回答护理、门店信息、预约政策和会员规则。

## Business Rule Boundary / 业务规则边界

RAG must not decide:

- service price
- service duration
- stylist availability
- schedule validation
- appointment creation
- conflict validation

这些结果必须由 `services/service_catalog.py`、SQLite 和后端预约服务决定。

## Error Contract / 错误契约

If MCP is unavailable, consultation returns HTTP 503 with `mcp_rag_unavailable`.

如果 MCP 不可用，咨询接口返回 HTTP 503 和 `mcp_rag_unavailable`。

The appointment API remains available because booking logic does not depend on MCP.

预约 API 保持可用，因为预约逻辑不依赖 MCP。

# Architecture / 系统架构

AI Hair Salon Agent is the business application. MCP Knowledge Service is the independent retrieval process used only for consultation knowledge.

AI Hair Salon Agent 是业务系统；MCP Knowledge Service 是独立知识检索进程，只服务咨询类知识问答。

## Runtime Responsibilities / 运行时职责

```text
User Request
  -> FastAPI
  -> Booking flow
     -> Deterministic booking service
     -> SQLite
     -> Optional Weather Context Tool only after conversational booking succeeds
  -> Consultation flow
     -> MCP Knowledge Gateway
     -> MCP Knowledge Service
     -> Dense Retrieval + BM25 + RRF
     -> citations
```

## Booking Flow / 预约路径

Booking APIs and conversational booking flows use deterministic backend services.

预约 API 和聊天预约流程都使用确定性后端规则。

```text
Booking request
  -> service_catalog
  -> stylist schedule validation
  -> business-hour validation
  -> conflict validation
  -> SQLite appointment write
  -> booking response
```

Booking rules decide:

- service normalization
- price and duration
- stylist availability
- appointment creation
- conflict detection
- booking status updates

预约成功、价格、时长、发型师可用性和冲突结果不由 RAG 或 LLM 决定。

## Consultation Flow / 咨询路径

Consultation uses MCP Knowledge Gateway and the external MCP Knowledge Service.

咨询类问题通过 MCP Knowledge Gateway 调用独立 MCP Knowledge Service。

```text
POST /api/consultation/query
  -> MCPKnowledgeGateway
  -> ClientSession.call_tool("query_knowledge_hub")
  -> MCP Knowledge Service
  -> Dense Retrieval + BM25 + RRF
  -> citations
  -> answer response
```

RAG retrieves:

- store information
- hair care guidance
- booking and cancellation policy
- membership and after-sales rules
- cited source metadata

If retrieved text differs from `services/service_catalog.py`, the deterministic service catalog wins.

如果检索文本与 `services/service_catalog.py` 不一致，以确定性服务目录为准。

## MCP Lifecycle / MCP 生命周期

When `RAG_MCP_ENABLED=true`, FastAPI lifespan startup creates `MCPKnowledgeGateway`.

当 `RAG_MCP_ENABLED=true` 时，FastAPI lifespan startup 会创建 `MCPKnowledgeGateway`。

The gateway reads:

- `RAG_MCP_SERVER_PYTHON`
- `RAG_MCP_SERVER_MODULE`
- `RAG_MCP_SERVER_CWD`
- `RAG_MCP_COLLECTION`
- `RAG_MCP_QUERY_TOP_K`

Startup sequence:

1. Start MCP Knowledge Service as a child process through stdio.
2. Call `initialize`.
3. Call `tools/list`.
4. Verify `query_knowledge_hub`.
5. Reuse the session for consultation requests.
6. Close the MCP child process during FastAPI shutdown.

启动顺序是通过 stdio 拉起 MCP 子进程、初始化、发现工具、校验 `query_knowledge_hub`，并在 FastAPI shutdown 时关闭子进程。

`python -m src.mcp_server.server` starts an stdio JSON-RPC server, not an interactive CLI.

For normal application use, let the MCP client launch it automatically.

For standalone verification, start it through an MCP client or verification script that sends `initialize`, `tools/list`, and tool calls.

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是可直接交互查询的 CLI。

正常业务运行时，应由 MCP Client 自动拉起该进程。

单独验证时，应通过 MCP Client 或验证脚本发送 `initialize`、`tools/list` 和 tool call，而不是只在终端直接运行该命令。

## MCP Failure Boundary / MCP 故障边界

When MCP is unavailable, consultation returns:

```json
{
  "code": "mcp_rag_unavailable",
  "message": "知识检索服务当前不可用，请稍后重试。",
  "trace_id": "..."
}
```

The appointment API does not depend on MCP and remains available.

预约 API 不依赖 MCP；MCP 不可用时，预约创建和冲突校验仍保持可用。

## Optional Weather Context Tool / 可选天气上下文工具

The Weather Context Tool is not an MCP Server, not an MCP Tool, and not part of RAG. It is an optional external real-time API lookup.

Weather Context Tool 不是 MCP Server、不是 MCP Tool，也不是 RAG 的一部分。它只是可选外部实时 API 上下文。

```text
Conversational booking saved successfully
  -> appointment success response
  -> optional weather context lookup
  -> append travel reminder only when real weather data is available
```

It never changes appointment success, service price, service duration, stylist selection, schedule availability, or conflict validation.

它不能改变预约是否成功、价格、时长、发型师选择、排班可用性或冲突校验结果。

If disabled, missing configuration, timeout, or non-200 weather response occurs, the system omits the weather reminder and keeps the booking success response unchanged.

如果未启用、缺少配置、超时或天气 API 返回非 200，系统只省略天气提醒，预约成功响应保持不变。

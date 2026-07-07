# Architecture / 系统架构

AI Hair Salon Agent separates consultation knowledge retrieval from deterministic booking decisions.

AI Hair Salon Agent 将咨询知识检索与确定性预约决策分离。

![Architecture](../architecture.svg)

## Entry and Routing / 入口与路由

`User Request` enters through Web pages or APIs, then reaches FastAPI routes, Swagger, `trace_id`, and health middleware.

用户请求通过 Web 页面或 API 进入 FastAPI，由 routes、Swagger、`trace_id` 和 health 相关逻辑处理。

The Task / Agent Layer handles classification, dialog state, and routing only. It does not decide price, duration, schedules, conflicts, or booking success.

Task / Agent Layer 只负责分类、对话和路由，不决定价格、时长、排班、冲突或预约成功。

## Consultation Flow / 咨询链路

Consultation uses the main project's MCP Knowledge Gateway, which is the internal MCP Client wrapper.

咨询链路使用主项目内部的 MCP Knowledge Gateway，即 MCP Client 封装。

```text
Consultation request
  -> MCP Knowledge Gateway
  -> MCP Knowledge Service (MCP Server)
  -> query_knowledge_hub
  -> Dense Retrieval + BM25 + RRF
  -> Consultation Result with Citations
```

When `RAG_MCP_ENABLED=true`, FastAPI lifespan startup creates the gateway, launches MCP Knowledge Service as a child process through stdio, calls `initialize`, calls `tools/list`, verifies `query_knowledge_hub`, and reuses the session for consultation requests.

当 `RAG_MCP_ENABLED=true` 时，FastAPI lifespan startup 会创建 gateway，通过 stdio 拉起 MCP Knowledge Service 子进程，执行 `initialize`、`tools/list`，校验 `query_knowledge_hub`，并复用该会话处理咨询请求。

`python -m src.mcp_server.server` starts an stdio JSON-RPC server, not an interactive CLI. For normal application use, let the MCP client launch it automatically.

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是可直接交互查询的 CLI；正常业务运行时应由 MCP Client 自动拉起。

## Booking Flow / 预约链路

Booking APIs and conversational booking flows use deterministic backend services and SQLite.

预约 API 和聊天预约流程使用确定性后端服务和 SQLite。

```text
Booking request
  -> service_catalog
  -> price and duration
  -> stylist schedule validation
  -> conflict validation
  -> SQLite appointment write
  -> Appointment Success
```

Booking rules decide:

- service normalization
- price and duration
- stylist availability
- appointment creation
- conflict detection
- booking status updates

预约成功、价格、时长、发型师可用性和冲突结果不由 RAG 或 LLM 决定。

If retrieved text differs from `services/service_catalog.py`, the deterministic service catalog wins.

如果检索文本与 `services/service_catalog.py` 不一致，以确定性服务目录为准。

## Optional Weather Context / 可选天气上下文

Optional Weather Context is not an MCP Server, not an MCP Tool, and not part of RAG.

Optional Weather Context 不是 MCP Server、不是 MCP Tool，也不是 RAG 的一部分。

```text
Appointment Success
  -> Optional Weather Context
  -> append travel reminder only when real weather data is available
```

It can run only after a conversational booking has already been saved. It never changes appointment success, service price, service duration, stylist selection, schedule availability, or conflict validation.

它只能在聊天预约保存成功后执行，不能改变预约是否成功、价格、时长、发型师选择、排班可用性或冲突校验结果。

If disabled, missing configuration, timeout, or non-200 weather response occurs, the system omits the weather reminder and keeps the booking success response unchanged.

如果未启用、缺少配置、超时或天气 API 返回非 200，系统只省略天气提醒，预约成功响应保持不变。

## Failure Boundaries / 故障边界

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

# Architecture

AI Hair Salon Agent is the business application. MCP Knowledge Service is the independent retrieval service.

## Runtime Flow

```text
User
  -> FastAPI
  -> API route / Agent boundary
  -> AppointmentService OR MCPKnowledgeGateway
  -> SQLite business data OR MCP Knowledge Service
  -> Response with trace_id
```

## Components

- FastAPI: HTTP API, web pages, Swagger, health endpoint, and request trace middleware.
- Task Classification Agent: classifies appointment, consultation, behavior, and unrelated requests.
- Appointment Agent/API: calls deterministic business services.
- AppointmentService: service catalog, price, duration, business hours, stylist schedules, and conflicts.
- MCPKnowledgeGateway: official MCP `ClientSession` and `stdio_client` wrapper.
- Consultation API: calls `query_knowledge_hub`, then optionally uses the chat model to generate a user-facing answer.
- User Behavior Agent: local analytics for service history and reminders. It is not a Memory system.
- Optional Weather Context Tool: optional external weather API lookup used only after a conversational booking has already succeeded.

## Responsibility Boundary

Deterministic backend:

- Service normalization.
- Price and duration.
- Appointment creation.
- Stylist availability.
- Conflict detection.
- Booking status updates.

MCP RAG:

- Store information.
- Hair care guidance.
- Booking and cancellation policy.
- Membership and after-sales rules.
- Source citations.

RAG does not decide appointment success. If retrieved text differs from `services/service_catalog.py`, the service catalog wins.

## MCP Failure Boundary

When MCP is unavailable, consultation returns:

```json
{
  "code": "mcp_rag_unavailable",
  "message": "知识检索服务当前不可用，请稍后重试。",
  "trace_id": "..."
}
```

The appointment API does not depend on MCP and remains available.

## Optional Weather Context Tool

The Weather Context Tool is not an MCP Server, not an MCP Tool, and not part of RAG. It is an optional external real-time API lookup.

Flow:

```text
Conversational booking saved successfully
  -> appointment success response
  -> optional weather context lookup
  -> append travel reminder only when real weather data is available
```

It never changes appointment success, service price, service duration, stylist selection, schedule availability, or conflict validation. If disabled, missing configuration, timeout, or non-200 weather response occurs, the system omits the weather reminder and keeps the booking success response unchanged.

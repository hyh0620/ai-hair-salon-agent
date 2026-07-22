---
name: run-demo
description: Run the verified five-minute AI Hair Salon Agent interview demo using live candidates returned by the configured demo environment.
---

# Run Demo

## Pipeline

1. Use a controlled demo database and ensure MCP Knowledge Service has already ingested `salon_knowledge`.
2. Configure the MCP interpreter, module, working directory and Collection, then start FastAPI. Its lifespan starts and manages the MCP stdio child process:
   ```bash
   python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers
   ```
3. Check health:
   ```bash
   curl http://127.0.0.1:8000/health
   ```
4. In the chat, enter `明天下午找擅长冷棕色的老师` and use the actual future candidates returned by the system. Do not assume a fixed date or stylist ID.
5. Select one returned option, confirm the summary, and verify that the response includes a real `appointment_id`. No database write should occur before final confirmation.
6. For an optional conflict demonstration, reuse the just-created stylist and time from the returned result with another test owner. Expect a conflict; do not edit the database to create it.
7. Query consultation:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/consultation/query \
     -H "Content-Type: application/json" \
     -d '{"question":"染发前后有什么注意事项？"}'
   ```
8. Confirm the response contains Citations and explain that MCP is the tool protocol while RAG is the retrieval pipeline.
9. `.env.example` already enables keyless Open-Meteo for Shanghai. Confirm weather appears only after the conversational booking commit; do not toggle it off after the demo. Hermetic verification blocks real weather calls.
10. Keep MCP failure injection and authentication replay demonstrations as optional Runbook flows, not part of the default five-minute demo.

## Output

- Health JSON.
- Dynamically selected appointment confirmation with a real ID.
- Optional conflict result based on the same returned candidate.
- Consultation answer with sources.
- Post-commit weather reminder when the optional service is available.

## Failure Handling

- If consultation returns 503, check MCP environment variables and collection ingestion; FastAPI normally owns the stdio child process.
- If no future candidate is returned, query another future period or rebuild the controlled demo database. Do not hard-code a replacement date or stylist.
- If the weather context is unavailable, the booking should still succeed and simply omit the weather reminder.
- See `docs/DEMO_RUNBOOK.md` for setup, failure injection and deep authentication validation.

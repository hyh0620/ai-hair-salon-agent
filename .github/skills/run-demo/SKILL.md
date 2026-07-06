---
name: run-demo
description: Run the verified local demo flow for AI Hair Salon Agent, including health, booking, conflict, consultation citations, and MCP failure behavior.
---

# Run Demo

## Pipeline

1. Ensure MCP Knowledge Service has ingested `salon_knowledge`.
2. Start FastAPI:
   ```bash
   python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```
3. Check health:
   ```bash
   curl http://127.0.0.1:8000/health
   ```
4. Create an appointment:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/appointment/create \
     -H "Content-Type: application/json" \
     -d '{"user_id":"demo_user","project":"男士短发","start_time":"2026-09-01 14:00","duration":"45分钟","stylist_id":1}'
   ```
5. Repeat with a different `user_id` and the same stylist/time. Expect HTTP 409.
6. Query consultation:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/consultation/query \
     -H "Content-Type: application/json" \
     -d '{"question":"染发前后有什么注意事项？"}'
   ```
7. Run runtime MCP failure check:
   ```bash
   python3.11 eval/mcp_runtime_failure_e2e.py --base-url http://127.0.0.1:8000 --timeout 60
   ```
8. Optional weather context demo:
   - Set `WEATHER_ENABLED=true`, `OPENWEATHER_API_KEY`, and `WEATHER_LOCATION` in a private `.env`.
   - Complete a conversational booking.
   - Confirm the weather reminder appears only after the booking success message.
   - Reset `WEATHER_ENABLED=false` for normal verification.

## Output

- Health JSON.
- Appointment confirmation.
- Conflict 409.
- Consultation answer with sources.
- MCP failure report.
- Optional weather reminder status when explicitly configured.

## Failure Handling

- If consultation returns 503 before the failure step, check MCP environment variables and collection ingestion.
- If the weather context is unavailable, the booking should still succeed and simply omit the weather reminder.

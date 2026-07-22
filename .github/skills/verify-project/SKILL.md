---
name: verify-project
description: Run the release verification checklist for AI Hair Salon Agent.
---

# Verify Project

## Pipeline

1. Dependency check:
   ```bash
   python -m pip check
   ```
2. Unit and contract tests:
   ```bash
   bash scripts/test_hermetic.sh
   ```
3. Start FastAPI and check:
   - `GET /health`
   - home page
   - Swagger
   - when RAG is enabled, FastAPI lifespan starts and manages the configured MCP stdio child process
4. Verify booking:
   - normal booking returns 200
   - duplicate stylist/time returns 409
5. Verify Hermetic consultation contracts without real Provider calls:
   - LLM-not-configured mode returns a retrieval summary
   - runtime MCP failure returns 503
   - Booking remains available during MCP failure
6. Verify weather context behavior:
   ```bash
   python -m pytest tests/test_weather_context_tool.py
   ```
   `.env.example` enables keyless Open-Meteo, but Hermetic tests use `EXTERNAL_CALL_POLICY=deny`. Confirm weather only follows successful conversational booking, failures never break booking, and REST or conflict booking does not call weather.
7. Run full evaluation only in its separately prepared environment:
   ```bash
   python eval/run_evaluation.py --timeout 120
   ```

## Output

- Verification summary.
- Failed command and error details, if any.

## Rules

- Do not print API keys.
- Do not commit runtime data or raw local reports.
- Do not describe pytest Mock results as real integration or Provider acceptance.
- Real Qwen, Embedding, MCP and Open-Meteo acceptance is an explicit isolated flow, not CI.
- Guest `anonymous_owner_id` is a browser-controlled business scope, not authentication.
- SQLite consistency checks cover one local database, not distributed transactions.

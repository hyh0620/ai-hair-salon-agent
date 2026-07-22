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
4. Verify booking:
   - normal booking returns 200
   - duplicate stylist/time returns 409
5. Verify consultation:
   - normal MCP consultation returns sources
   - LLM-not-configured mode returns retrieval summary
   - runtime MCP failure returns 503
6. Verify optional weather context behavior:
   ```bash
   python -m pytest tests/test_weather_context_tool.py
   ```
   Confirm weather failures never break booking and structured conflict booking does not call weather.
7. Run full evaluation:
   ```bash
   python eval/run_evaluation.py --timeout 120
   ```

## Output

- Verification summary.
- Failed command and error details, if any.

## Rules

- Do not print API keys.
- Do not commit runtime data or raw local reports.

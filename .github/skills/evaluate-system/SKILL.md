---
name: evaluate-system
description: Run the reproducible evaluation suite and explain functional contract results separately from retrieval quality.
---

# Evaluate System

## Pipeline

1. Start normal app on port 8000.
2. Start MCP-disabled app on port 8002:
   ```bash
   RAG_MCP_ENABLED=false DATABASE_URL=sqlite:////tmp/salon_eval_8002.db \
     python -m uvicorn app:app --host 127.0.0.1 --port 8002 --no-proxy-headers
   ```
3. Start LLM-disabled app on port 8003 using placeholder model variables.
4. Run:
   ```bash
   NO_PROXY=127.0.0.1,localhost python eval/run_evaluation.py \
     --base-url http://127.0.0.1:8000 \
     --mcp-unavailable-base-url http://127.0.0.1:8002 \
     --llm-unconfigured-base-url http://127.0.0.1:8003 \
     --timeout 120
   ```

## Output

- Local summary report.
- Local per-case report.
- Functional contract counts.
- Retrieval quality metrics.

## Rules

- Do not present pytest mock results as real integration results.
- Do not commit raw local reports.

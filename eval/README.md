# Evaluation Suite

This directory contains the reproducible evaluation workflow for AI Hair Salon Agent.

## Files

- `golden_dataset.jsonl`: 28 golden cases.
- `run_evaluation.py`: runs real API checks against running app instances.
- `report_generator.py`: writes summary and per-case reports locally.
- `mcp_runtime_failure_e2e.py`: verifies runtime MCP failure behavior.

Raw report outputs under `eval/reports/` are local runtime artifacts and are not committed.

## Run

Start the normal app, an MCP-disabled app, and an LLM-disabled app, then run:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

The runner separates functional contracts from retrieval quality. It does not use subjective LLM grading.

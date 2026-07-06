# Evaluation

The evaluation suite separates functional contracts from retrieval quality.

## Files

- `eval/golden_dataset.jsonl`: 28-case golden dataset.
- `eval/run_evaluation.py`: real API evaluation runner.
- `eval/report_generator.py`: summary and per-case report writer.
- `eval/mcp_runtime_failure_e2e.py`: runtime MCP failure check.
- `tests/`: pytest contracts and deterministic service tests.

Raw local report files are intentionally not committed because they may contain local paths, trace IDs, and environment details.

## Dataset

- Booking: 8 cases
- RAG consultation: 8 cases
- Routing/tool selection: 6 cases
- Exception/degradation: 6 cases

## Metrics

Functional contract metrics:

- Expected HTTP status.
- Expected route.
- Expected tool/service selection.
- Expected booking result.
- Expected error contract.

Retrieval quality metrics:

- Hit@1.
- Hit@3.
- MRR.
- Citation presence.
- Expected-source citation match.
- Empty-result handling.

## Verified Summary

- Functional Contract: 28 / 28
- Booking contract: 9 / 9
- Booking success: 3 / 3
- Conflict block: 2 / 2
- Invalid booking rejection: 4 / 4
- Route accuracy: 6 / 6
- RAG cases evaluated: 11
- Hit@1: 10 / 11
- Hit@3: 11 / 11
- MRR: 0.9545
- Citation expected-source match: 11 / 11
- Empty result handling: 1 / 1
- MCP unavailable handling: 1 / 1

## Knowledge Corpus Used For The Summary

- Salon source documents: 7
- Semantic chunks: 24
- ChromaDB vectors: 24
- BM25 documents: 24
- Collection: `salon_knowledge`

## Reproduce

Run pytest:

```bash
python3.11 -m pip check
python3.11 -m pytest
```

Run full evaluation:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

Run runtime MCP failure check:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/mcp_runtime_failure_e2e.py \
  --base-url http://127.0.0.1:8000 \
  --timeout 60
```

## Limits

The dataset is designed for regression evidence, not broad benchmark claims. Do not claim production accuracy or perfect retrieval.

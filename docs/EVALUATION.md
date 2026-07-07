# Evaluation / 评估

The evaluation suite separates functional API contracts from retrieval quality metrics.

评估体系把功能契约和检索质量分开计算，避免把 API 可用性结果误写成 RAG 准确率。

## Files / 文件

- `eval/golden_dataset.jsonl`: 28-case golden dataset.
- `eval/run_evaluation.py`: real API evaluation runner.
- `eval/report_generator.py`: summary and per-case report writer.
- `eval/mcp_runtime_failure_e2e.py`: runtime MCP failure check.
- `tests/`: pytest contracts and deterministic service tests.

Raw local report files are intentionally not committed because they may contain local paths, trace IDs, and environment details.

原始本地报告可能包含本机路径、trace_id 和环境细节，因此不提交到公开仓库。

## Dataset / 数据集

| Category | Count |
| --- | ---: |
| Booking | 8 |
| RAG consultation | 8 |
| Routing/tool selection | 6 |
| Exception/degradation | 6 |

## Functional Contract / 功能契约

Functional Contract checks whether the public API behavior matches the expected contract.

功能契约关注接口行为是否符合预期，不等同于检索排序质量。

Examples:

- expected HTTP status
- expected route
- expected tool/service selection
- expected booking result
- expected error contract
- conflict booking rejection
- MCP unavailable error contract

## Retrieval Quality / 检索质量

Retrieval Quality evaluates whether expected source documents appear in returned citations.

检索质量关注预期来源是否出现在返回结果和 citations 中。

Metrics:

- Hit@1
- Hit@3
- MRR
- citation presence
- expected-source citation match
- empty-result handling

## Verified Evaluation Snapshot / 已验证评估快照

| Metric | Result |
| --- | --- |
| Functional Contract | 28 / 28 |
| RAG cases | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |
| MCP failure | Consultation returns 503, booking remains available |

Additional regression counts:

- Booking contract: 9 / 9
- Booking success: 3 / 3
- Conflict block: 2 / 2
- Invalid booking rejection: 4 / 4
- Route accuracy: 6 / 6
- Empty result handling: 1 / 1
- MCP unavailable handling: 1 / 1

## Knowledge Corpus / 知识库规模

| Item | Value |
| --- | ---: |
| Salon source documents | 7 |
| Semantic chunks | 24 |
| ChromaDB vectors | 24 |
| BM25 documents | 24 |
| Collection | `salon_knowledge` |

This is a small controlled corpus for reproducible regression evidence.

这是小型受控语料，用于可复现回归验证；不能声明生产准确率或通用 benchmark。

## Reproduce / 复现

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

## Limits / 限制

- The dataset is regression evidence, not a broad benchmark.
- Retrieval quality is reported separately from functional contracts.
- No production accuracy or perfect retrieval claim is made.

该评估只证明当前公开版本在受控样本上的行为，不代表生产环境泛化效果。

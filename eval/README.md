# 评估套件

本目录包含 AI Hair Salon Agent 的可复现评估流程。

## 文件

- `golden_dataset.jsonl`：28 条基准用例。
- `run_evaluation.py`：针对运行中的应用实例执行真实 API 检查。
- `report_generator.py`：在本地生成摘要和逐用例报告。
- `mcp_runtime_failure_e2e.py`：验证 MCP 运行时故障行为。

`eval/reports/` 下的原始报告属于本地运行产物，不提交到仓库。

## 运行

启动正常应用、禁用 MCP 的应用和未配置 LLM 的应用，然后执行：

```bash
NO_PROXY=127.0.0.1,localhost python eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

评估器分别统计功能契约和检索质量，不使用主观的 LLM 评分。

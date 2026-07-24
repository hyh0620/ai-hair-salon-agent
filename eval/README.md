# 评估套件

本目录包含 AI Hair Salon Agent 的可复现评估流程。

## 文件

- `golden_dataset.jsonl`：28 条基准用例。
- `dataset_resolver.py`：为一次运行统一解析未来工作日占位符。
- `run_evaluation.py`：针对运行中的应用实例执行真实 API 检查。
- `evaluation_metrics.py`：计算功能契约和去重后的来源级检索指标。
- `report_generator.py`：在本地生成摘要和逐用例报告。
- `verified_snapshot.py`：构建去敏的公开评估快照。
- `verify_snapshot.py`：从逐用例明细重新计算快照指标。
- `mcp_runtime_failure_e2e.py`：验证 MCP 运行时故障行为。
- `snapshots/`：仅保存完整真实评估通过后生成的可验证快照。

`eval/reports/` 下的原始报告属于本地运行产物，不提交到仓库。

预约用例使用 `{{EVAL_DATE_DAY_1}}` 和 `{{EVAL_DATE_DAY_2}}`。执行器在一轮评估开始时统一将它们解析为未来工作日，并把实际日期和每条用例的已解析时间写入报告。

## 运行

启动正常应用、禁用 MCP 的应用和未配置 LLM 的应用，然后执行：

```bash
NO_PROXY=127.0.0.1,localhost python eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --verified-snapshot-dir eval/snapshots \
  --timeout 120
```

评估器分别统计功能契约和检索质量，不使用主观的 LLM 评分。B005 会先通过创建 API 获得真实 `appointment_id` 和 `version`，再调用正式修改 API，并通过公开排班 API 验证原档期释放和新档期占用。

只有所有真实用例完整执行、功能契约通过且工作区干净时才会生成快照。验证命令：

```bash
python eval/verify_snapshot.py \
  eval/snapshots/verified_<UTC_DATE>_<SHORT_SHA>.json
```

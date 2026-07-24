# 已验证评估快照

此目录只用于提交完整真实评估生成的去敏快照。

生成条件：

* 28 条基准用例全部执行，没有跳过项；
* 功能契约全部通过；
* 真实 LLM、Embedding、MCP/RAG 和故障模式服务均已配置；
* Git 工作区干净，快照可关联到确定的提交；
* 快照通过 `eval/verify_snapshot.py` 从逐用例明细重新计算聚合指标。

生成命令示例：

```bash
python eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --verified-snapshot-dir eval/snapshots

python eval/verify_snapshot.py \
  eval/snapshots/verified_<UTC_DATE>_<SHORT_SHA>.json
```

未满足完整真实评估条件时，执行器拒绝生成快照。快照不包含提示词、原始模型回复、身份标识、数据库记录、凭据、Trace 或本机绝对路径。

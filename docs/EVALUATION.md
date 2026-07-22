# 测试与评估

项目将自动化回归、功能契约和 RAG 检索质量分开统计，避免把接口可用性结果误写成检索准确率。

## 评估文件

* `eval/golden_dataset.jsonl`：28 条 Golden Dataset；
* `eval/run_evaluation.py`：真实 API 评估执行器；
* `eval/report_generator.py`：摘要和逐用例报告生成器；
* `eval/mcp_runtime_failure_e2e.py`：MCP 运行时故障验证；
* `tests/`：pytest 功能契约、事务、并发和确定性服务测试。

本地原始报告可能包含文件路径、`trace_id` 和环境细节，因此不提交到公开仓库。

## Golden Dataset 分类

| 分类 | 数量 |
| --- | ---: |
| Booking | 8 |
| RAG consultation | 8 |
| Routing / tool selection | 6 |
| Exception / degradation | 6 |

## 功能契约

Functional Contract 检查公开行为是否符合预期，包括：

* HTTP status；
* 任务 route；
* tool 或 service 选择；
* Booking 结果；
* 错误契约；
* 重复档期冲突拒绝；
* MCP 不可用时的降级响应。

Functional Contract 不是 RAG 准确率。它证明特定输入下的接口与业务契约，不衡量检索排序质量。

## RAG 检索质量

RAG 指标检查预期来源是否出现在知识服务返回结果和 Citations 中：

* Hit@1；
* Hit@3；
* MRR；
* Citation 是否存在；
* Citation expected-source match；
* 空结果处理。

这些指标只针对当前小型受控语料。Citation 匹配说明预期来源被引用，不代表答案在生产环境中完全正确。

## 已保存的评估快照

| 指标 | 结果 |
| --- | ---: |
| Functional Contract | 28 / 28 |
| RAG Cases | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |

MCP failure 场景的已保存结果为：Consultation 返回 503，Booking 保持可用。

附加功能回归计数：

| 项目 | 结果 |
| --- | ---: |
| Booking contract | 9 / 9 |
| Booking success | 3 / 3 |
| Conflict block | 2 / 2 |
| Invalid booking rejection | 4 / 4 |
| Route accuracy | 6 / 6 |
| Empty result handling | 1 / 1 |
| MCP unavailable handling | 1 / 1 |

以上数字来自仓库中已保存的 Verified Evaluation Snapshot。重新运行完整评估需要对应的本地服务与知识语料，不应把历史快照描述为本次 pytest 的输出。

## 知识库规模

| 项目 | 数量 |
| --- | ---: |
| Salon source documents | 7 |
| Semantic chunks | 24 |
| ChromaDB vectors | 24 |
| BM25 documents | 24 |
| Collection | `salon_knowledge` |

这是用于可复现回归的小型受控语料，不代表生产语料规模或通用 Benchmark。

## 自动化回归

主项目正式环境为 Python 3.12。使用已验证依赖创建开发环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install \
  -c constraints-py312.txt \
  -r requirements-dev.txt

python -m pip check
python -m pytest -W error::DeprecationWarning
python -m compileall agents api services db config eval
```

当前主分支基线：

```text
403 passed
0 failed
0 warnings
```

普通测试使用临时 SQLite、Fake/Mock LLM、Mock Weather、Mock MCP 和固定时间，不访问用户数据库或真实外部服务。

## 完整评估命令

以下命令使用已激活的 Python 3.12 环境。运行前需要按[本地运行与深度演示 Runbook](DEMO_RUNBOOK.md)启动对应服务：

```bash
NO_PROXY=127.0.0.1,localhost python eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

验证 MCP 运行时故障边界：

```bash
NO_PROXY=127.0.0.1,localhost python eval/mcp_runtime_failure_e2e.py \
  --base-url http://127.0.0.1:8000 \
  --timeout 60
```

## 结果边界

* Functional Contract 与 RAG 指标分别统计；
* Hit@1、Hit@3 和 MRR 只评估当前受控语料；
* Citation 来源匹配不等于答案在生产环境完全正确；
* 当前结果用于回归证据，不是通用 Benchmark；
* 项目不基于该快照声称生产准确率或完美检索。

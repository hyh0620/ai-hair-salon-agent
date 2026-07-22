# 测试与评估

项目将自动化回归、功能契约和 RAG 检索质量分开统计，避免把接口可用性结果误写成检索准确率。

## 评估文件

* `eval/golden_dataset.jsonl`：28 条基准用例；
* `eval/run_evaluation.py`：真实 API 评估执行器；
* `eval/report_generator.py`：摘要和逐用例报告生成器；
* `eval/mcp_runtime_failure_e2e.py`：MCP 运行时故障验证；
* `tests/`：pytest 功能契约、事务、并发和确定性服务测试。

本地原始报告可能包含文件路径、`trace_id` 和环境细节，因此不提交到公开仓库。

## 基准用例集分类

基准用例集（Golden Dataset）共 28 条。

| 分类 | 数量 |
| --- | ---: |
| 预约流程 | 8 |
| RAG 知识咨询 | 8 |
| 路由与工具选择 | 6 |
| 异常与降级 | 6 |

## 功能契约

功能契约（Functional Contract）检查公开行为是否符合预期，包括：

* HTTP 状态码；
* 任务路由；
* 工具或服务选择；
* 预约结果；
* 错误契约；
* 重复档期冲突拒绝；
* MCP 不可用时的降级响应。

功能契约不是 RAG 准确率。它证明特定输入下的接口与业务契约，不衡量检索排序质量。

## RAG 检索质量

RAG 指标检查预期来源是否出现在知识服务返回结果和引用来源（Citations）中：

* Hit@1；
* Hit@3；
* MRR；
* 引用来源是否存在；
* 引用来源与预期来源匹配；
* 空结果处理。

这些指标只针对当前小型受控语料。引用匹配说明预期来源被引用，不代表答案在生产环境中完全正确。

## 已保存的评估快照

| 指标 | 结果 |
| --- | ---: |
| 功能契约 | 28 / 28 |
| RAG 用例 | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| 引用来源与预期来源匹配 | 11 / 11 |

MCP 故障场景的已保存结果为：知识咨询返回 503，预约功能保持可用。

附加功能回归计数：

| 项目 | 结果 |
| --- | ---: |
| 预约契约 | 9 / 9 |
| 预约成功 | 3 / 3 |
| 冲突阻止 | 2 / 2 |
| 无效预约拒绝 | 4 / 4 |
| 路由准确 | 6 / 6 |
| 空结果处理 | 1 / 1 |
| MCP 不可用处理 | 1 / 1 |

以上数字来自仓库中已保存的已验证评估快照（Verified Evaluation Snapshot）。重新运行完整评估需要对应的本地服务与知识语料，不应把历史快照描述为本次 pytest 的输出。

## 知识库规模

| 项目 | 数量 |
| --- | ---: |
| 门店知识源文档 | 7 |
| 语义切片 | 24 |
| ChromaDB 向量 | 24 |
| BM25 文档 | 24 |
| 知识集合 | `salon_knowledge` |

这是用于可复现回归的小型受控语料，不代表生产语料规模或通用基准测试。

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
0 个失败
0 个警告
```

普通测试使用临时 SQLite、模拟 LLM、模拟天气服务、模拟 MCP 服务和固定时间，不访问用户数据库或真实外部服务。

## 完整评估命令

以下命令使用已激活的 Python 3.12 环境。运行前需要按[本地运行与深度演示手册](DEMO_RUNBOOK.md)启动对应服务：

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

* 功能契约与 RAG 指标分别统计；
* Hit@1、Hit@3 和 MRR 只评估当前受控语料；
* 引用来源匹配不等于答案在生产环境完全正确；
* 当前结果用于回归证据，不是通用基准测试；
* 项目不基于该快照声称生产准确率或完美检索。

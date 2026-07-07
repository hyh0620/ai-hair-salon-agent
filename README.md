# AI Hair Salon Agent

AI hair salon appointment and consultation system that separates deterministic booking logic from MCP-based knowledge retrieval.

面向理发店预约与咨询场景，将价格、时长、排班和冲突校验保留在确定性后端，将护理、政策和门店知识交给独立 MCP RAG 服务。

## Overview / 项目概述

AI Hair Salon Agent is a FastAPI application for salon appointment booking and consultation. It uses LangChain 1.x for LLM-facing flows, SQLite for structured business data, and MCP Knowledge Service for cited knowledge retrieval.

项目重点不是让 LLM 决定交易结果，而是把大模型能力限制在意图识别、信息抽取、追问和自然语言回复中。预约是否成功、价格是多少、服务多久、发型师是否可用，均由后端确定性规则处理。

## Core Capabilities / 核心能力

| Capability | English | 中文说明 |
| --- | --- | --- |
| Deterministic Booking | Service catalog, price, duration, business-hour checks, stylist schedules, and conflict validation are handled by backend services and SQLite. | 预约交易链路由确定性后端负责，避免 LLM 改写价格、时长、排班或冲突结果。 |
| MCP Knowledge Retrieval | Consultation requests use an official MCP Python client to call MCP Knowledge Service. | 咨询类问题通过 MCP Client 调用独立知识服务，业务系统不内置第二套 RAG。 |
| Hybrid Retrieval | MCP Knowledge Service returns Dense Retrieval + BM25 + RRF results with citations. | 护理、政策、门店说明等非结构化知识由混合检索和来源引用支撑。 |
| Evaluation and Failure Boundaries | 28-case evaluation, retrieval metrics, health checks, trace_id, and MCP runtime failure isolation are included. | 评估结果区分功能契约和检索质量，并验证 MCP 断开时咨询返回 503、预约仍可用。 |
| Optional Weather Context Tool | A separate external weather API can append a post-booking travel reminder when configured. | 天气工具只在预约保存成功后补充出行提醒，不属于 MCP、RAG 或预约核心逻辑。 |

## Architecture / 系统架构

![Architecture](./architecture.svg)

```text
User Request
  -> FastAPI
  -> Booking flow
     -> Deterministic booking service
     -> SQLite
     -> Optional Weather Context Tool only after booking succeeds
  -> Consultation flow
     -> MCP Knowledge Gateway
     -> MCP Knowledge Service
     -> Dense + BM25 + RRF
     -> Citations
```

When MCP is enabled, FastAPI launches MCP Knowledge Service as a child process through stdio using the configured Python interpreter, module, and working directory.

启用 MCP 后，FastAPI 会根据配置中的 Python 解释器、模块路径和工作目录，通过 stdio 启动 MCP Knowledge Service 子进程。

`python -m src.mcp_server.server` starts an stdio JSON-RPC server, not an interactive CLI.

For normal application use, let the MCP client launch it automatically.

For standalone verification, start it through an MCP client or verification script that sends `initialize`, `tools/list`, and tool calls.

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是可直接交互查询的 CLI。

正常业务运行时，应由 MCP Client 自动拉起该进程。

单独验证时，应通过 MCP Client 或验证脚本发送 `initialize`、`tools/list` 和 tool call，而不是只在终端直接运行该命令。

## System Boundaries / 系统职责边界

| Area | Responsibilities | 中文边界 |
| --- | --- | --- |
| LLM | Intent classification, slot extraction, missing-information follow-up, and natural-language generation from retrieved context. | LLM 负责理解和表达，不负责最终业务裁决。 |
| Deterministic backend | Normalize service names, compute price and duration from `services/service_catalog.py`, validate schedules, create appointments, and block conflicts. | 价格、时长、排班、创建预约和冲突校验必须由后端规则决定。 |
| MCP RAG | Retrieve care guidance, store information, booking policy, membership rules, and citations from the configured collection. | 知识库用于咨询回答，不决定预约是否成功。 |
| Optional Weather Context Tool | When explicitly configured, append a short weather reminder after a conversational booking has already been saved. | 天气失败时只省略提醒，不能影响预约成功、价格、时长或冲突结果。 |

## Technology Stack / 技术栈

- Python 3.11
- FastAPI, Uvicorn
- LangChain 1.x
- Qwen or another OpenAI-compatible chat provider
- SQLAlchemy and SQLite
- Official MCP Python SDK
- MCP Knowledge Service with ChromaDB, BM25, RRF, and citations
- pytest

## Project Structure / 项目结构

```text
ai-hair-salon-agent/
├── api/
├── agents/
├── config/
├── db/
├── services/
├── web/
├── tests/
├── eval/
├── docs/
├── .github/skills/
├── AGENTS.md
├── .env.example
├── LICENSE
├── README.md
├── requirements.txt
└── app.py
```

## Related Repository / 关联项目

MCP Knowledge Service: <https://github.com/hyh0620/mcp-knowledge-service>

The AI Hair Salon Agent uses MCP Knowledge Service as an external knowledge retrieval process.

主项目通过 MCP Client 调用该独立知识服务，用于咨询类知识检索。

## Quick Start / 快速启动

### Booking-only local start / 仅预约功能本地启动

The default `.env.example` keeps MCP disabled and API keys empty, so the local app can start safely for booking APIs and deterministic service tests.

默认 `.env.example` 关闭 MCP 且不包含真实 Key，因此复制后可以先安全启动 booking-only 版本。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Useful endpoints:

- Home: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`
- Stylists: `http://127.0.0.1:8000/stylists`
- Stylist schedule: `http://127.0.0.1:8000/stylist-schedule`
- Knowledge status: `http://127.0.0.1:8000/knowledge`

### Full consultation demo / 完整咨询演示

Prepare MCP Knowledge Service first:

```bash
cd <PATH_TO_MCP_KNOWLEDGE_SERVICE>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/settings.example.yaml config/settings.yaml
```

Configure local provider environment variables in a private `.env` file or shell environment. Then ingest the salon example:

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

Set the business app `.env`:

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

After updating `.env`, restart FastAPI so its lifespan startup creates a new MCP gateway with MCP enabled.

修改 `.env` 后，需要重启 FastAPI，使 lifespan startup 按启用后的 MCP 配置重新创建 MCP gateway。

When MCP is enabled, FastAPI launches MCP Knowledge Service as a child process through stdio using the configured Python interpreter, module, and working directory.

启用 MCP 后，FastAPI 会根据配置中的 Python 解释器、模块路径和工作目录，通过 stdio 启动 MCP Knowledge Service 子进程。

`python -m src.mcp_server.server` starts an stdio JSON-RPC server, not an interactive CLI.

For normal application use, let the MCP client launch it automatically.

For standalone verification, start it through an MCP client or verification script that sends `initialize`, `tools/list`, and tool calls.

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是可直接交互查询的 CLI。

正常业务运行时，应由 MCP Client 自动拉起该进程。

单独验证时，应通过 MCP Client 或验证脚本发送 `initialize`、`tools/list` 和 tool call，而不是只在终端直接运行该命令。

### Optional Weather Context Tool / 可选天气上下文工具

```env
WEATHER_ENABLED=false
OPENWEATHER_API_KEY=
WEATHER_LOCATION=
WEATHER_TIMEOUT_SECONDS=3
```

Leave weather disabled for normal tests and demos unless you intentionally want to show an external context API. Do not commit a real weather API key.

### CORS / 跨域配置

```env
CORS_ALLOWED_ORIGINS=
```

Cross-origin access is disabled by default. Configure explicit origins through `CORS_ALLOWED_ORIGINS` only when a separate frontend origin is needed.

默认同源运行不启用跨域；只有前端与 API 使用不同 origin 时，才通过 `CORS_ALLOWED_ORIGINS` 显式配置允许来源。

## Tests / 测试

```bash
python3.11 -m pip check
python3.11 -m pytest
```

Default pytest uses mocks and deterministic local services. It does not call a real Qwen API or require real API keys. Weather tests also use mocks and do not call the real OpenWeather service.

## Evaluation / 评估结果

### Run Evaluation / 运行评估

Start three app instances for the full evaluation:

```bash
DATABASE_URL=sqlite:////tmp/salon_eval_8000.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000

RAG_MCP_ENABLED=false DATABASE_URL=sqlite:////tmp/salon_eval_8002.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8002

env -u LLM_API_KEY -u LLM_BASE_URL -u LLM_MODEL \
DATABASE_URL=sqlite:////tmp/salon_eval_8003.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8003
```

Then run:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

The public repository does not include raw local evaluation reports.

### Verified Evaluation Snapshot / 已验证评估快照

| Metric | Result |
| --- | --- |
| Functional Contract | 28 / 28 |
| RAG cases evaluated | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |
| MCP runtime failure | Consultation returns 503 while booking remains available |

## MCP Failure Boundary Demo / MCP 故障边界演示

Start one normal app with MCP enabled, then run:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/mcp_runtime_failure_e2e.py \
  --base-url http://127.0.0.1:8000 \
  --timeout 60
```

Expected behavior:

- Consultation works before the MCP child process is terminated.
- The script terminates the real MCP child process.
- Consultation returns HTTP 503 with `code=mcp_rag_unavailable`.
- Booking creation still returns 200.
- Duplicate stylist/time booking still returns 409.

## Known Limits / 当前限制

| Limit | 中文说明 |
| --- | --- |
| Small controlled corpus: 7 documents, 24 chunks. | 当前知识库规模较小，适合验证链路和评估方法，不代表生产规模。 |
| No production deployment claim. | README 不声明已经生产上线或支持真实用户规模。 |
| No Rerank, Ragas, Graph RAG, Memory, multimodal, Docker/K8s claim. | 未验证能力不写入公开能力范围。 |
| Weather tool is optional and not part of MCP capability. | 天气工具只是预约成功后的可选上下文提醒，不用于证明 MCP 能力。 |

## Documentation / 文档

- [Architecture / 系统架构](docs/ARCHITECTURE.md)
- [Evaluation / 评估](docs/EVALUATION.md)
- [Demo Guide / 演示指南](docs/DEMO_GUIDE.md)
- [RAG Service Integration / RAG 服务集成](docs/RAG_SERVICE_INTEGRATION.md)

## Skills / 项目工作流

Operational skills are stored in `.github/skills/`:

- `setup-environment`
- `run-demo`
- `evaluate-system`
- `update-salon-knowledge`
- `verify-project`

They describe repeatable project workflows and do not contain credentials.

## Security / 安全说明

Do not commit `.env`, API keys, local runtime databases, ChromaDB files, BM25 indexes, logs, trace files, or raw local evaluation dumps.

Cross-origin access is disabled by default. Configure explicit origins through `CORS_ALLOWED_ORIGINS` only when a separate frontend origin is needed.

默认同源运行不启用跨域；只有前端与 API 使用不同 origin 时，才通过 `CORS_ALLOWED_ORIGINS` 显式配置允许来源。

# AI Hair Salon Agent

## 智能预约与服务咨询系统

这是一个基于 FastAPI、LangChain 1.x、MCP、RAG、SQLite 的理发店 AI 应用。系统将 Booking 与 Consultation 拆成两条独立链路：LLM 负责意图识别（Intent Classification）、信息提取、槽位收集（Slot Filling）和自然语言生成；价格、服务时长、营业时间、发型师可用性、冲突校验与最终预约结果由确定性后端（Deterministic Backend）处理。

独立的 MCP Knowledge Service 只提供护理、政策和门店知识检索，不参与预约裁决。该边界便于单独测试业务规则，也能把 MCP / RAG 故障限制在咨询链路。

## 项目简介

项目覆盖理发店的预约创建与知识咨询：预约请求由 Agent 完成理解和路由，再交给 `SERVICE_CATALOG`、`AppointmentService` 与 SQLite 执行确定性校验；咨询请求通过 `MCPKnowledgeGateway` 调用外部 MCP Knowledge Service，使用混合检索（Hybrid Retrieval）返回来源引用（Citations）。

项目重点是明确生成式能力与交易规则的边界，而不是让模型直接决定业务结果。

## 为什么拆分 Booking 与 Consultation

| 链路 | 数据来源与处理方式 | 错误边界 |
| --- | --- | --- |
| Booking | `SERVICE_CATALOG`、排班数据、营业时间和 SQLite 预约记录；结果由确定性规则计算。 | 价格、时长、可用性和冲突必须可复现，不能由 LLM 或 RAG 猜测。 |
| Consultation | MCP Knowledge Service 中的护理、门店说明、预约政策和会员规则；适合检索后组织自然语言回答。 | MCP 不可用时咨询返回明确错误，但不阻断 Booking。 |

两类任务的数据来源、错误容忍度和验收方式不同。拆分后可以分别验证 API 契约、预约规则和检索质量，并防止生成模型越过最终业务裁决边界。

## 核心能力

| 能力 | 实现与边界 |
| --- | --- |
| Agent 任务路由 | 识别预约、咨询和其他请求；负责对话、信息提取和缺失槽位追问。 |
| 确定性预约 | `SERVICE_CATALOG` 提供标准价格与标准时长；`AppointmentService` 与 SQLite 处理营业时间、发型师排班、可用性、保存和冲突校验。 |
| 模糊偏好与真实排班 | 将“明天下午找擅长冷棕色的老师”等请求识别为 `search_availability`，规范化日期、时间范围、服务和专长，再基于 SQLite 忙碌时段生成稳定候选。 |
| MCP 知识检索 | `MCPKnowledgeGateway` 是主项目中的 MCP Client 封装，使用官方 MCP Python SDK 连接独立 MCP Knowledge Service。 |
| 混合检索与引用 | 外部知识服务组合向量检索（Dense Retrieval）、BM25 和倒数排名融合（Reciprocal Rank Fusion, RRF），返回 Citations。 |
| 故障隔离 | MCP 运行时不可用时，Consultation 返回 HTTP 503；Booking 继续使用本地确定性后端。 |
| 可选天气提醒 | Optional Weather Context Tool 仅在聊天预约保存成功后调用外部天气 API；失败时省略提醒，不改变预约结果。 |

## 系统架构

![系统架构](./architecture.svg)

- Consultation：`MCPKnowledgeGateway` 调用独立 MCP Knowledge Service，返回带 Citations 的咨询结果。
- Booking：`SERVICE_CATALOG`、`AppointmentService` 和 SQLite 决定价格、时长、排班、冲突及预约结果。
- Weather：只在预约成功后作为非阻塞后处理执行，不属于 MCP 或 RAG。

## 两条核心链路

### 确定性预约链路

```text
User Request
  -> Task / Agent Layer
  -> SERVICE_CATALOG（标准价格、标准时长）
  -> AppointmentService（营业时间、排班、可用性、冲突校验）
  -> SQLite（预约与发型师排班数据）
  -> Appointment Success / HTTP 4xx
  -> Optional Weather Context Tool（仅成功后，可选且非阻塞）
```

Agent / LLM 可以理解服务、日期、时间和可选偏好，但不能决定最终价格、最终时长、发型师是否可用、是否冲突或预约是否成功。预约状态会分别保存日期、精确时间和时间范围；LLM 输出的时间还需经过当前用户输入的确定性校验，不能把只有日期的请求补成午夜或默认营业时间。

标准服务不要求用户提供时长。服务项目一旦明确，`SERVICE_CATALOG` 会补充标准时长和价格。精确日期与时间齐全后才能直接创建预约；日期、服务和时间范围齐全时则进入 `AvailabilityService`，基于真实排班返回候选。

```text
“预约明天”
  -> 保存日期，继续询问服务和时间
“男士短发”
  -> SERVICE_CATALOG 补充45分钟和88元，继续询问时间
“下午两点”
  -> 组合为明天14:00 -> 排班与冲突校验 -> SQLite
```

可用性搜索采用自然语言驱动的确定性预约工作流：Agent 负责识别日期、模糊时段和偏好；`AvailabilityService` 按服务支持、结构化专长、营业时间和 SQLite 排班生成候选。候选只保存在当前 session，用户选择后还需最终确认；确认时再次检查冲突，成功写入后才调用可选天气工具。

```text
“明天下午找擅长冷棕色的老师”
  -> search_availability
  -> 明天 + 12:00-18:00 + 染发 + 冷棕色
  -> 真实发型师资料 + SQLite 排班
  -> 候选选择 -> 最终确认 -> 冲突复查 -> SQLite
```

### MCP / RAG 咨询链路

```text
POST /api/consultation/query
  -> MCPKnowledgeGateway.query_knowledge
  -> ClientSession.call_tool("query_knowledge_hub")
  -> Dense Retrieval + BM25 + RRF
  -> Citations
  -> LLM 组织回答，或在未配置 LLM 时返回检索摘要
```

价格和服务时长问题仍以 `services/service_catalog.py` 为最终事实来源。RAG 不决定价格、时长、营业时间、排班、冲突或预约结果。

## 技术栈

- Python 3.11、FastAPI、Uvicorn
- LangChain 1.x、OpenAI-compatible Chat Model（可配置 Qwen）
- Official MCP Python SDK：`ClientSession`、`stdio_client`
- SQLAlchemy、SQLite
- MCP Knowledge Service：ChromaDB、BM25、RRF、Citations
- pytest

## 项目结构

```text
ai-hair-salon-agent/
├── api/                    # FastAPI 路由与响应模型
├── agents/                 # 分类、预约和咨询 Agent
├── config/                 # 模型、数据库、时间与 trace 配置
├── db/                     # SQLAlchemy 模型与 Repository
├── services/               # 预约、服务目录、发型师与 MCP Gateway
├── web/                    # 页面模板与静态资源
├── tests/                  # 单元测试与回归测试
├── eval/                   # Golden Dataset 与真实评估脚本
├── docs/                   # 架构、评估、演示和集成文档
├── architecture.svg
├── .env.example
├── requirements.txt
└── app.py
```

## 快速开始

### Booking-only 本地启动

默认 `.env.example` 关闭 MCP 且不包含真实 API Key，可以先运行预约 API 和确定性业务逻辑。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

常用入口：

- 首页：`http://127.0.0.1:8000/`
- Swagger：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`
- 发型师：`http://127.0.0.1:8000/stylists`
- 发型师排班：`http://127.0.0.1:8000/stylist-schedule`
- 知识服务状态：`http://127.0.0.1:8000/knowledge`

### 完整 Consultation 演示

先准备独立的 [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)：

```bash
cd <PATH_TO_MCP_KNOWLEDGE_SERVICE>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

执行真实 Embedding 前，需要在知识服务的本地 `.env` 中配置 Provider Key，且不要提交该文件。

在主项目 `.env` 中启用集成：

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

修改 `.env` 后需要重启 FastAPI。启用 MCP 后，FastAPI lifespan 会按配置通过 stdio 拉起 MCP Knowledge Service 子进程，执行 `initialize` 和 `tools/list`，并复用 `ClientSession`；应用关闭时清理子进程。

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是交互式 CLI。正常业务运行由 MCP Client 自动拉起；单独验证时需要 MCP Client 或验证脚本发送 `initialize`、`tools/list` 和 tool call。

## 配置说明

| 配置 | 作用 | 默认边界 |
| --- | --- | --- |
| `DATABASE_URL` | SQLite 连接 | 本地业务数据，不提交运行时数据库。 |
| `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` | Chat Model | 未配置时，确定性 Booking 不受影响；Consultation 可返回检索摘要。 |
| `RAG_MCP_ENABLED` | 是否启用 MCP Knowledge Service | 默认 `false`。只有路径和 Provider 准备完成后再启用。 |
| `RAG_MCP_SERVER_PYTHON`、`RAG_MCP_SERVER_CWD` | MCP 子进程解释器与工作目录 | 必须指向有效的独立知识服务 checkout。 |
| `RAG_MCP_COLLECTION` | 咨询使用的 collection | 示例使用 `salon_knowledge`。 |
| `WEATHER_ENABLED`、`WEATHER_PROVIDER`、`WEATHER_LOCATION_NAME`、`WEATHER_LATITUDE`、`WEATHER_LONGITUDE`、`WEATHER_TIMEZONE` | 可选天气上下文 | 默认使用无需 API Key 的 Open-Meteo 上海预报；仅在预约写入成功后查询，失败不影响预约。 |
| `CORS_ALLOWED_ORIGINS` | 显式跨域 allowlist | 默认空值，仅同源；不使用 wildcard。 |

真实 Key 只放在本地 `.env`，不要提交到 Git。

## API 与使用示例

创建预约：

```bash
curl -X POST http://127.0.0.1:8000/api/appointment/create \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo_user",
    "project": "男士短发",
    "start_time": "2026-07-15 14:00",
    "duration": "45分钟",
    "style_preference": "渐变推剪"
  }'
```

咨询知识问题：

```bash
curl -X POST http://127.0.0.1:8000/api/consultation/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"染发后如何减少掉色？"}'
```

Consultation 响应包含 `answer`、`sources`、`retrieval_mode`、`collection`、`rag_status`、`llm_status`、`source_count` 和 `trace_id`。

聊天页面也支持模糊偏好与排班查询：

```text
用户：明天下午找擅长冷棕色的老师
系统：返回真实可预约候选及时间、专长、时长和价格
用户：第一个
系统：展示具体候选并请求最终确认
用户：确认
系统：二次检查冲突，写入 SQLite，返回 appointment_id，并可选追加上海天气提醒
```

“冷棕色适合什么肤色？”等知识问题仍进入 Consultation；RAG 不回答“谁有空”或具体档期。

## 测试与历史评估结果

默认 pytest 使用 mock 和本地确定性服务，不调用真实 Qwen、OpenWeather 或外部 MCP 服务。

```bash
python3.11 -m pip check
python3.11 -m pytest
```

已保存的历史评估结果如下；本次 README 修改未重新运行评估：

| 历史指标 | 结果 |
| --- | ---: |
| Functional Contract | 28 / 28 |
| Booking contract | 9 / 9 |
| Booking success | 3 / 3 |
| Conflict block | 2 / 2 |
| Invalid booking rejection | 4 / 4 |
| RAG cases | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |

历史故障用例记录显示：MCP 子进程运行中断开后，Consultation 返回 HTTP 503，正常 Booking 仍可创建，同一发型师同一时段的冲突仍返回 HTTP 409。指标定义、样本分母和复现方式见 [检索与业务评估](docs/EVALUATION.md)。

## 故障边界

- MCP 启动失败、连接断开或 tool call 失败：Consultation 返回 HTTP 503 和 `mcp_rag_unavailable`，不静默回退到旧本地 FAISS。
- LLM 未配置：不影响确定性 Booking；MCP 可用时 Consultation 返回整理后的检索摘要和 Citations。
- Optional Weather Context Tool 缺少配置、超时或 HTTP 错误：只省略天气提醒，不撤销已保存预约。
- 非法时间、营业时间外或预约冲突：由后端返回明确的 HTTP 4xx，不交给 LLM 修正业务结果。

## 项目边界与已知限制

- 当前语料是小型受控数据集：7 份文档、24 个 chunks，用于链路验证和回归评估，不代表生产规模或通用 benchmark。
- 项目不声明已经生产部署，也不声明真实用户规模、SLA、高并发或自动恢复能力。
- 公开范围不包含 Rerank、Ragas、Graph RAG、Memory、multimodal、Docker/K8s、认证授权或 production multi-tenancy。
- MCP Knowledge Service 是可选的独立咨询依赖，不参与 Booking；Weather Tool 也不是 MCP 或 RAG。
- 历史指标来自仓库保存的评估文档，不能解释为本次现场测试或生产准确率。

## 相关仓库

- [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)：通过 MCP Client 接入的独立知识检索服务。

## 相关文档

- [系统架构（Architecture）](docs/ARCHITECTURE.md)
- [检索与业务评估（Evaluation）](docs/EVALUATION.md)
- [演示说明（Demo Guide）](docs/DEMO_GUIDE.md)
- [MCP RAG 集成说明（Integration）](docs/RAG_SERVICE_INTEGRATION.md)

## 安全说明

不要提交 `.env`、API Key、本地 SQLite、ChromaDB、BM25 index、日志、trace 或原始评估报告。跨域默认关闭；只有前端与 API 使用不同 origin 时，才通过 `CORS_ALLOWED_ORIGINS` 配置显式 allowlist。

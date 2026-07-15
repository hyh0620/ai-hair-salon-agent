# AI Hair Salon Agent

## 自然语言驱动的理发店预约与知识咨询 Agentic Workflow

这是一个基于 FastAPI、LangChain 1.x、MCP、RAG 和 SQLite 的理发店 AI 应用。系统把 Booking 与 Consultation 拆成两条独立链路：Agent 负责理解自然语言、补全槽位和管理多轮交互，确定性后端负责价格、时长、发型师专长、真实排班、冲突和最终预约结果。

独立的 MCP Knowledge Service 提供门店知识与护理咨询，但不参与预约裁决。项目重点不是构建“完全自主 Agent”，而是展示如何让概率性的语言理解安全接入可验证的业务 Workflow。

> **Agent 负责理解，Workflow 负责执行，SQLite 负责事实。**

## 项目定位

传统小程序适合用户已经知道服务、发型师和时间的场景。本项目处理的是更接近真实对话的输入：

- 模糊自然语言和不完整需求；
- 相对日期、精确时间与模糊时段；
- 服务偏好与发型师专长；
- 多轮 Slot Filling、候选选择和最终确认；
- 预约业务与知识咨询之间的动态路由。

典型输入包括：

```text
明天下午找擅长冷棕色的老师
今天下午哪些理发师有空？
预约明天，男士短发，下午两点
```

Agent 将这些表达转换为结构化业务约束，再由 `SERVICE_CATALOG`、`AvailabilityService`、`AppointmentService` 和 SQLite 执行确定性预约流程。

## 系统架构

![System Architecture](./architecture.svg)

### Agent / Dialog Layer

负责意图识别、Slot Filling、相对日期与时间理解、专长偏好提取、多轮状态管理和候选选择理解。当前核心意图包括 `create_booking`、`search_availability` 和 `consultation`。

### Deterministic Booking Backend

`SERVICE_CATALOG` 提供标准价格和时长；`AvailabilityService` 根据服务支持、发型师专长、营业时间和 SQLite 排班生成候选；`AppointmentService` 负责最终营业时间、冲突校验与预约写入。

未指定发型师时，精确时间和时间范围都会先返回真实候选，不会自动分配第一位发型师。用户选择并最终确认后才写入 SQLite。

### MCP / RAG Consultation

`MCPKnowledgeGateway` 通过官方 MCP Client 调用独立 MCP Knowledge Service。知识服务使用 Dense Retrieval、BM25 和 RRF 检索门店说明、护理知识和服务政策，并返回 Citations。

**MCP/RAG 不参与真实排班、价格、时长、冲突或预约结果判断。** Optional Weather Context Tool 只在预约保存成功后查询上海天气，失败不会撤销预约。

## 为什么不是纯 Workflow

| 场景 | 适合的方案 |
| --- | --- |
| 用户明确选择服务、发型师和时间 | 小程序或固定 Workflow |
| 用户表达模糊、不完整或包含多个偏好 | Agent 进行自然语言理解与 Slot Filling |
| 价格、时长、排班和冲突判断 | 确定性后端 |
| 最终预约写入 | `AppointmentService` + SQLite |

本项目不是用 Agent 替代 Workflow，而是用 Agent 把自然语言转换为结构化业务约束，再交给确定性 Workflow 执行。

## 核心能力

- 隐式预约意图识别：`create_booking` / `search_availability` / `consultation`；
- 相对日期、精确时间和上午/下午/晚上时段解析；
- 结构化服务目录、标准价格、标准时长和专长映射；
- 基于 SQLite 的真实排班查询与冲突过滤；
- 精确时间和时间范围共用 `AvailabilityService`；
- 多轮 Slot Filling 与 session 隔离；
- 候选选择、最终确认和确认时二次冲突检查；
- 重复确认幂等，不重复写入预约；
- MCP Hybrid RAG 与 Citations；
- 预约成功后的上海 Open-Meteo 天气提醒。

## 三个核心演示

### 场景一：模糊偏好预约

```text
用户：明天下午找擅长冷棕色的老师

系统：
→ 识别 search_availability
→ 解析明天、12:00—18:00、染发、冷棕色专长
→ 查询结构化发型师资料和 SQLite 排班
→ 返回真实候选
→ 用户选择并最终确认
→ 二次冲突检查后保存预约
```

### 场景二：多轮补全

```text
用户：预约明天
系统：询问服务和具体时间

用户：男士短发
系统：从 SERVICE_CATALOG 取得45分钟和88元，只继续询问时间

用户：下午两点
系统：查询14:00真实可用发型师，不自动分配

用户：第一个
系统：展示具体候选并请求最终确认

用户：确认
系统：再次检查冲突，写入 SQLite，返回 appointment_id
```

### 场景三：咨询与预约边界

```text
今天下午哪些理发师有空？
→ Appointment / AvailabilityService

男士短发适合什么脸型？
→ Consultation / MCP RAG / Citations
```

## 核心设计原则

### 概率性理解，确定性执行

LLM 可以理解表达、提取约束和组织回复，但不得决定价格、标准时长、发型师是否存在、是否空闲或预约是否成功。

### Session 状态优先

当 session 中存在未完成预约时，`男士短发`、`第一个`、`确认` 等短消息必须结合当前状态解释，不能只按单句重新分类。后端会覆盖前端错误 route。

### 两阶段确认

```text
查询候选 → 用户选择 → 最终确认 → 二次冲突检查 → SQLite 保存
```

候选只保存在当前 session；搜索和选择阶段不写数据库，也不调用天气。

### 故障隔离

- MCP 不可用时 Consultation 返回明确错误，Booking 继续使用本地业务服务；
- 天气服务失败时只省略提醒，不改变已保存预约；
- LLM 未配置时，结构化预约 API 和确定性业务服务仍可运行。

## 技术栈

- Python 3.11、FastAPI、Uvicorn
- LangChain 1.x、OpenAI-compatible LLM / Qwen
- Official MCP Python SDK
- SQLAlchemy、SQLite
- ChromaDB、BM25、RRF、Citations
- Open-Meteo
- pytest

## 项目结构

```text
ai-hair-salon-agent/
├── agents/                 # 路由、预约与咨询 Agent
├── api/                    # FastAPI 接口
├── services/               # Availability、Appointment、MCP Gateway
├── db/                     # SQLAlchemy 与 SQLite
├── web/                    # 聊天和排班页面
├── tests/                  # 单元测试与回归测试
├── docs/                   # 架构、评估、演示和集成文档
├── architecture.svg
└── app.py
```

## 快速开始

默认 `.env.example` 关闭 MCP，可先启动 Booking-only 本地版本：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

常用入口：

- 首页：`http://127.0.0.1:8000`
- Swagger：`http://127.0.0.1:8000/docs`
- 发型师排班：`http://127.0.0.1:8000/stylist-schedule`

完整知识服务配置与 ingestion 步骤见 [MCP/RAG 集成说明](docs/RAG_SERVICE_INTEGRATION.md)。

## 测试

```bash
python3.11 -m pip check
python3.11 -m pytest
```

当前本地回归结果：**108 passed，0 failed**。普通测试使用临时 SQLite、Fake/Mock LLM、Mock Weather、Mock MCP 和冻结时间，不依赖真实外部服务。

评估集设计、检索指标和历史结果见 [评估方法与结果](docs/EVALUATION.md)。

## 文档入口

- [系统架构](docs/ARCHITECTURE.md)
- [演示指南](docs/DEMO_GUIDE.md)
- [评估方法与结果](docs/EVALUATION.md)
- [MCP/RAG 集成说明](docs/RAG_SERVICE_INTEGRATION.md)

相关仓库：

- [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)

## 项目边界

这是用于展示 Agentic Workflow、MCP/RAG 集成和确定性预约设计的工程项目。当前使用小型受控知识库和本地 SQLite，不声称已生产部署、拥有真实商业流量或具备未经验证的生产基础设施能力。

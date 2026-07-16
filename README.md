# AI Hair Salon Agent

## 自然语言驱动的理发店预约与知识咨询系统

这是一个基于 FastAPI、LangChain 1.x、MCP、RAG 和 SQLite 的理发店 AI 应用，支持自然语言预约、真实排班查询、多轮信息补全、候选选择，以及预约查询、取消、改期和知识咨询。

Agent 负责理解用户表达、提取约束并维护对话状态；确定性业务服务负责价格、时长、发型师匹配、排班、冲突和预约写入；独立的 MCP Knowledge Service 只负责知识咨询。

> **Agent 负责理解，业务服务负责决策，SQLite 负责事实。**

## 项目简介

理发店预约中的输入往往包含相对日期、模糊时段、服务偏好和多轮补充信息，例如：

```text
明天下午找擅长冷棕色的老师
今天下午哪些理发师有空？
我想预约明天做男士短发
周五晚上想染发，预算四百左右
```

系统持续补全业务意图、日期、精确时间或时间范围、服务项目、指定发型师、专长偏好、候选选择和最终确认。路由不仅依据当前消息，也会结合当前 Session 中已保存的预约状态。

## 项目目标

项目用自然语言连接预约、可用性搜索、预约生命周期和知识咨询，将模型输出转换为可校验的结构化约束，再使用真实发型师资料和 SQLite 排班执行确定性业务规则。预约创建、取消和修改共享 `AppointmentService`；MCP 用于解耦知识检索，Booking、Consultation 和 Optional Weather 具有独立故障边界。

## 系统架构

![System Architecture](./architecture.svg)

### Agent / Dialog Layer

负责意图识别、Slot Filling、相对日期、精确时间和模糊时段解析、服务与专长偏好提取、多轮 Session 状态管理，以及候选选择和确认语义理解。除 `create_booking`、`search_availability` 和 `consultation` 外，聊天路由还识别预约查询、取消、修改和改期意图。

### Deterministic Booking Backend

- `SERVICE_CATALOG` 提供标准服务、价格和时长；
- `AvailabilityService` 根据结构化发型师资料和 SQLite 排班生成真实候选；
- `AppointmentService` 负责所有权、状态、版本、营业时间、服务能力和冲突校验，以及最终预约写入；
- SQLite 保存发型师、排班和预约事实。

未指定发型师时，系统不会自动选择第一位可用人员。精确时间和时间范围都会先通过 `AvailabilityService` 返回候选，用户选择并最终确认后才写入 SQLite。

预约取消和修改使用同一 SQLite `BEGIN IMMEDIATE` 事务更新 `appointments` 与 `stylist_schedules`。客户端提交查询时获得的 `version`，最终写入时再次校验；过期版本返回 `stale_state`，避免静默覆盖其他修改。

### MCP / RAG Consultation

`MCPKnowledgeGateway` 使用官方 MCP Client 调用独立 MCP Knowledge Service。知识服务面向门店说明、服务政策和护理知识，检索链路包括 Dense Retrieval、BM25、RRF 和 Citations。

MCP/RAG 不参与真实排班、价格、时长、冲突或预约结果判断。天气工具只在预约成功写入并获得真实 `appointment_id` 后调用；天气不可用时不会撤销预约。

## 核心预约流程

```text
User Message
  ↓
Intent Routing
  ↓
Slot Filling
  ├── 日期、精确时间 / 时间范围
  └── 服务、发型师、专长偏好
  ↓
SERVICE_CATALOG
  └── 标准价格与时长
  ↓
AvailabilityService
  ├── 服务支持、专长与营业时间
  └── SQLite 排班、冲突过滤与候选排序
  ↓
Candidate Selection
  ↓
Final Confirmation
  ↓
Second Conflict Check
  ↓
AppointmentService → SQLite → Appointment ID
  ↓
Optional Weather Reminder
```

预约生命周期使用同一确定性服务：

```text
List / Get Own Appointments
  ↓
Select Appointment + Expected Version
  ↓
Final Confirmation
  ↓
BEGIN IMMEDIATE
  ├── Owner / Status / Version Check
  ├── Time / Service / Conflict Check
  ├── Update Appointment
  └── Update Schedule
  ↓
COMMIT or Full ROLLBACK
```

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| 自然语言意图 | 识别 `create_booking`、`search_availability`、`consultation`；没有“预约”二字的档期查询也进入预约业务链路 |
| 日期与时间槽位 | 日期、精确时间和时间范围分别保存；只有日期不会补成午夜，“下午”不会被擅自转换为固定开始时间 |
| 服务目录 | `SERVICE_CATALOG` 提供标准价格和时长，例如男士短发为 45 分钟、88 元 |
| 专长映射 | 将 `冷棕`、`冷棕色`、`冷调棕色` 规范化为染发服务下的冷棕色专长 |
| 真实排班搜索 | 综合服务支持、专长、营业时间、完整服务时长、SQLite 已有预约、过去时段过滤和稳定排序 |
| 候选选择 | 候选保存在当前 Session，支持序号、姓名、姓名加时间等表达；歧义选择会继续追问 |
| 最终确认 | 用户选择后仍需确认；确认时再次检查营业时间、服务支持和冲突，然后写入 SQLite |
| Session 与幂等 | 不同 Session 不共享候选；活动预约状态优先；重复“确认”不会重复创建预约 |
| 预约生命周期 | 查询当前调用者的预约；取消会保留记录并释放档期；修改或改期会重新计算时长、价格并复查冲突 |
| 并发一致性 | 创建、取消和修改使用数据库事务；`version` 提供乐观并发校验，SQLite Trigger 防止重叠 busy 排班 |
| Consultation | 护理和服务知识通过 MCP/RAG 检索，并返回来源 Citations |

## 交互示例

### 场景一：模糊偏好预约

```text
用户：明天下午找擅长冷棕色的老师
系统：识别 search_availability
     → 解析日期、下午时段、染发和冷棕色专长
     → 查询结构化发型师资料和 SQLite 排班
     → 返回真实候选
     → 用户选择并最终确认
     → 二次冲突检查后保存预约
```

### 场景二：多轮信息补全

```text
用户：预约明天
系统：记录日期，询问服务和时间
用户：男士短发
系统：从 SERVICE_CATALOG 取得45分钟和88元，只继续询问时间
用户：下午两点
系统：查询明天14:00的真实候选，不自动分配发型师
用户：第一个
系统：展示具体候选并请求最终确认
用户：确认
系统：再次检查冲突，写入 SQLite，返回 appointment_id
```

### 场景三：预约与咨询路由

```text
今天下午哪些理发师有空？
→ Appointment / AvailabilityService

男士短发适合什么脸型？
→ Consultation / MCP RAG / Citations
```

## 设计原则

### 概率性理解，确定性执行

LLM 用于理解自然语言、提取业务约束、维护多轮交互和组织回复，但不决定标准价格、标准时长、发型师是否存在、是否支持服务、是否空闲或预约是否成功。

### Session 状态优先

`男士短发`、`下午两点`、`第一个`、`确认` 等短消息需要结合当前 Session 解释。最终路由决定保留在后端，可覆盖前端传入的错误 route。

### 业务事实单一来源

| 业务事实 | 最终来源 |
| --- | --- |
| 服务、价格、时长 | `SERVICE_CATALOG` |
| 发型师资料与专长 | 结构化发型师数据 |
| 已有预约与排班 | SQLite |
| 可用候选 | `AvailabilityService` |
| 冲突检查与预约写入 | `AppointmentService` |
| 预约所有权、状态与版本 | `AppointmentService` + SQLite |
| 护理知识与服务政策 | MCP Knowledge Service |

### 故障隔离

- MCP 不可用时，Booking 仍通过本地确定性业务服务运行；
- 天气服务失败时，只省略提醒，不改变已保存预约；
- RAG 不用于生成真实排班或预约结果；
- 数据库保存失败时，不返回预约成功；
- LLM 未配置时，结构化预约接口和确定性业务服务仍可独立测试与运行。

## 技术栈

- Python 3.12（主要开发与运行版本；CI 同时验证 3.11）、FastAPI、Uvicorn
- LangChain 1.x、OpenAI-compatible LLM / Qwen
- Official MCP Python SDK
- SQLAlchemy、SQLite
- ChromaDB、BM25、Reciprocal Rank Fusion、Citations
- Open-Meteo、pytest

## 项目结构

```text
ai-hair-salon-agent/
├── agents/                 # 路由、预约、咨询与多轮状态
├── api/                    # FastAPI 业务接口
├── config/                 # 应用配置
├── db/                     # SQLAlchemy 与 SQLite
├── services/               # Availability、Appointment、MCP Gateway
├── web/                    # 聊天、状态与排班页面
├── tests/                  # 单元测试与回归测试
├── eval/                   # Golden Dataset 与评估脚本
├── docs/                   # 架构、演示、评估与集成文档
├── architecture.svg
├── requirements.txt
└── app.py
```

## 快速开始

主要开发与运行版本为 Python 3.12；迁移期 CI 同时验证 Python 3.11 和 3.12。

默认 `.env.example` 关闭 MCP，可先启动 Booking-only 本地版本：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install "starlette<1" "pytest>=8,<9" httpx
cp .env.example .env
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

第二条安装命令是迁移期兼容措施；正式依赖约束将在后续依赖治理中统一。

常用入口：

- 首页：`http://127.0.0.1:8000`
- Swagger：`http://127.0.0.1:8000/docs`
- 发型师信息：`http://127.0.0.1:8000/stylists`
- 发型师排班：`http://127.0.0.1:8000/stylist-schedule`
- 健康检查：`http://127.0.0.1:8000/health`

完整知识服务配置与 ingestion 步骤见 [MCP / RAG 集成说明](docs/RAG_SERVICE_INTEGRATION.md)。

## 测试与评估

### 回归测试

```bash
python -m pip check
python -m pytest
```

| 项目 | 结果 |
| --- | ---: |
| pytest | 181 passed |
| Failed | 0 |

CI 在 Python 3.11 和 3.12 上运行完整测试集。普通测试使用临时 SQLite、Fake / Mock LLM、Mock Weather、Mock MCP 和冻结时间，不依赖真实用户数据库或真实外部服务。

### 已验证评估快照

项目使用独立 Golden Dataset，将功能契约与 RAG 检索质量分别评估。仓库中保存的 Verified Evaluation Snapshot 为：

| 指标 | 结果 |
| --- | ---: |
| Functional Contract | 28 / 28 |
| RAG Cases | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |

当前知识库为小型受控语料，包含 7 份源文档和 24 个语义切片。这些结果用于可复现回归验证，不代表生产环境准确率，也不是通用 Benchmark。

详细方法和结果见 [评估方法与完整结果](docs/EVALUATION.md)。

## 文档入口

- [系统架构](docs/ARCHITECTURE.md)
- [演示指南](docs/DEMO_GUIDE.md)
- [评估方法与完整结果](docs/EVALUATION.md)
- [MCP / RAG 集成说明](docs/RAG_SERVICE_INTEGRATION.md)
- [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)

## 项目边界

该项目用于展示自然语言 Agent、确定性预约服务、MCP/RAG 知识检索和多轮业务状态管理的完整集成。

当前系统使用小型受控知识库和本地 SQLite，重点验证架构设计、业务边界和端到端流程，不声称已经生产部署、拥有真实商业流量或具备未经验证的生产基础设施能力。

预约生命周期中的 `user_id`（聊天场景为 Session 标识）用于当前调用者范围内的业务所有权校验；它不是登录认证，不等同于生产级身份系统。

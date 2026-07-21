# 演示指南

本指南用于在 3 至 5 分钟内展示自然语言预约、确定性排班、知识咨询和故障边界。

## 环境准备

演示涉及两个独立仓库：

* `ai-hair-salon-agent`：FastAPI 业务应用，主要开发与运行版本为 Python 3.12；
* `mcp-knowledge-service`：独立 MCP Knowledge Service，使用自己的虚拟环境，其 `pyproject.toml` 要求 Python `>=3.11`。

两个项目不要求使用完全相同的 Python 小版本。不要提交本地 `.env`、运行数据、日志、SQLite 数据库或向量索引。

## 启动主项目

```bash
cd <PATH_TO_AI_HAIR_SALON_AGENT>

python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install \
  -c constraints-py312.txt \
  -r requirements.txt

cp .env.example .env

python -m uvicorn app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --no-proxy-headers
```

`.env.example` 默认 `RAG_MCP_ENABLED=false`，因此可以先运行 Booking 和本地页面，不依赖 MCP Knowledge Service。

常用入口：

* 首页：`http://127.0.0.1:8000`
* Swagger：`http://127.0.0.1:8000/docs`
* 系统状态：`http://127.0.0.1:8000/status`
* 排班页面：`http://127.0.0.1:8000/stylist-schedule`
* 健康检查：`http://127.0.0.1:8000/health`

## 准备独立 MCP Knowledge Service

知识咨询演示需要单独准备 `mcp-knowledge-service`：

```bash
cd <PATH_TO_MCP_KNOWLEDGE_SERVICE>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config/settings.example.yaml config/settings.yaml
```

根据知识服务自身文档配置 Provider，再导入受控语料：

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是交互式查询 CLI。正常业务运行时，由主项目的 MCP Client 自动拉起并管理该子进程。

在主项目私有 `.env` 中启用：

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

修改后重启 FastAPI。应用 lifespan 会创建 `MCPKnowledgeGateway`，通过 stdio 启动服务，执行 `initialize` 和 `list_tools`，确认 `query_knowledge_hub` 后复用当前 MCP Session。

## 天气配置

`.env.example` 中的天气默认配置为：

```env
WEATHER_ENABLED=true
WEATHER_PROVIDER=open_meteo
WEATHER_LOCATION_NAME=上海
```

Open-Meteo 不需要 API Key。天气只在聊天预约已经成功写入并取得真实 `appointment_id` 后调用；失败只省略提醒，不影响预约结果。天气不属于 MCP 或 RAG，REST 预约创建接口也不会自动追加天气。

## 3 至 5 分钟演示顺序

### 1. 展示架构边界

打开 README 和架构图，说明：

```text
Agent负责理解
业务服务负责决策
SQLite负责保存事实
```

强调 `TaskClassificationAgent`、`AppointmentAgent` 和 `ConsultantAgent` 是中心路由协调的职责组件，不是分布式自主 Agent。

### 2. 模糊档期查询

在同一浏览器 Session 输入：

```text
明天下午找擅长冷棕色的老师
```

预期：

* 进入 `search_availability`，不进入 Consultation；
* 将“明天”“下午”“冷棕色”规范化为日期、时间范围、染发和专长；
* 使用结构化发型师资料和 SQLite 排班生成真实候选；
* 此时不写数据库，不调用 MCP，也不调用天气。

### 3. 候选选择与最终确认

继续输入：

```text
第一个
确认
```

系统先展示具体候选并请求最终确认。确认时再次检查服务能力、营业时间和冲突，随后在同一事务中写入预约和排班，返回真实 `appointment_id`。聊天链路可在成功后追加上海天气。

如果演示数据库当前没有候选，不要修改真实数据制造结果；改用测试环境或选择数据库中真实可用的未来日期。

### 4. 多轮槽位补全

清空对话后输入：

```text
预约明天
男士短发
下午两点
```

应观察到：

1. 第一轮只保存日期，不生成默认小时；
2. 第二轮从 `SERVICE_CATALOG` 取得 45 分钟和 88 元，仍等待时间；
3. 第三轮查询明天 14:00 的真实发型师候选，不自动分配第一位人员；
4. 选择并确认后才写入 SQLite。

### 5. 并发与冲突保护

展示已占用档期或重复请求时，说明最终确认会再次检查数据库。`BEGIN IMMEDIATE`、SQLite Trigger 和 `version` 保护当前单 SQLite 数据库中的一致性。

这里展示的是单应用实例和单数据库的并发保护，不是分布式锁或分布式事务。

### 6. 知识咨询与 Citations

输入：

```text
冷棕色适合什么肤色？
```

预期进入 Consultation：

```text
ConsultantAgent
→ MCPKnowledgeGateway
→ query_knowledge_hub
→ Dense Retrieval + BM25 + RRF
→ 回答与 Citations
```

MCP 负责标准化调用独立服务；RAG 是知识服务内部的检索与融合。RAG 不提供真实排班，也不决定预约结果。

### 7. 故障与项目边界

可以展示 `eval/mcp_runtime_failure_e2e.py` 已覆盖的故障契约：MCP 不可用时 Consultation 返回 503，而 Booking 仍可运行。

最后说明当前 owner 范围校验和 Session 的真实边界：

* `owner_id` 来自客户端 `user_id` 或聊天 Session ID，不是可信认证身份；
* Session 是进程内对话状态，不是持久化 Memory 或 Redis Session；
* 生产环境需要认证系统、共享 Session 和服务型数据库。

## 建议演示问题

```text
今天下午哪些理发师有空？
明天下午找擅长冷棕色的老师
预约明天
男士短发
下午两点
染发前后有什么注意事项？
烫发后多久可以洗头？
门店几点营业？
```

## 可以说明的能力

* 规则预路由、Session 状态和 LLM 辅助理解协同工作；
* 价格、标准时长、真实排班和预约结果由确定性服务控制；
* 未指定发型师时先返回候选，用户选择并确认后才写入；
* 创建、取消和修改共享原子事务与乐观并发校验；
* MCP Knowledge Service 提供带 Citations 的 RAG 知识咨询；
* 天气是成功预约后的非阻塞上下文增强；
* 已保存评估快照中 Functional Contract 为 28 / 28，RAG 指标单独报告。

## 不应夸大的能力

* 不声称已经生产部署或拥有真实商业流量；
* 不把 owner 范围校验描述为安全认证；
* 不把进程内 Session 描述为长期 Memory 或分布式 Session；
* 不把 SQLite 事务描述为分布式并发方案；
* 不把职责拆分的 Agent 组件描述为分布式自主多 Agent；
* 不声称 RAG 完美或评估结果是通用 Benchmark；
* 不把天气描述为 MCP Tool 或 RAG 组件。

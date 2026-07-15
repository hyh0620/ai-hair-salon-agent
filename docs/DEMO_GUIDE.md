# Demo Guide / 演示指南

## Prerequisites / 前置条件

Two independent repositories are expected:

- `ai-hair-salon-agent`
- `mcp-knowledge-service`

Both should use Python 3.11. Do not commit local `.env`, runtime data, logs, or vector indexes.

两个项目保持独立：主项目是业务应用，MCP Knowledge Service 是独立知识检索服务。不要提交本地 `.env`、运行时数据、日志或向量索引。

Weather reminders are optional. Keep `WEATHER_ENABLED=false` unless you are explicitly demonstrating the external Weather Context Tool with a private API key.

天气提醒是可选外部上下文工具；默认保持 `WEATHER_ENABLED=false`。

## Prepare MCP Knowledge Service / 准备 MCP Knowledge Service

This step prepares the external knowledge service checkout, dependencies, provider configuration, and `salon_knowledge` ingestion.

本步骤只准备 MCP Knowledge Service 的本地目录、依赖、Provider 配置和 `salon_knowledge` 导入。

```bash
cd <PATH_TO_MCP_KNOWLEDGE_SERVICE>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/settings.example.yaml config/settings.yaml
```

Set provider credentials in a private `.env` or shell environment, then ingest:

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

`python -m src.mcp_server.server` starts an stdio JSON-RPC server, not an interactive CLI.

For normal application use, let the MCP client launch it automatically.

For standalone verification, start it through an MCP client or verification script that sends `initialize`, `tools/list`, and tool calls.

`python -m src.mcp_server.server` 启动的是 stdio JSON-RPC Server，不是可直接交互查询的 CLI。

正常业务运行时，应由 MCP Client 自动拉起该进程。

单独验证时，应通过 MCP Client 或验证脚本发送 `initialize`、`tools/list` 和 tool call，而不是只在终端直接运行该命令。

## Start Business App / 启动业务应用

```bash
cd <PATH_TO_AI_HAIR_SALON_AGENT>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

With the default `.env.example`, MCP is disabled and booking-only APIs can start safely.

默认 `.env.example` 关闭 MCP，可先安全启动 booking-only 版本。

For the full consultation demo, set:

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

## Three-Minute Demo / 三分钟演示顺序

1. Open `/health` and show app, database, MCP RAG, collection, and LLM status.
2. Open `/docs` and show separate appointment and consultation APIs.
3. Create a normal appointment and show price, duration, stylist, and status.
4. Repeat the same stylist/time and show HTTP 409.
5. Ask a consultation question and show citations.
6. Run `eval/mcp_runtime_failure_e2e.py` and show consultation 503 while booking remains available.
7. Optional Weather Context Tool defaults to `WEATHER_ENABLED=true` with Open-Meteo and the configured Shanghai coordinates. It requires no API Key, runs only after a booking is saved, and is not part of MCP or RAG.

## Preference-Based Availability Demo / 模糊偏好排班演示

在首页使用同一个浏览器 session 依次输入：

```text
明天下午找擅长冷棕色的老师
第一个
确认
```

第一步应进入 Booking，而不是 Consultation。系统把“明天”“下午”“冷棕色”规范化为日期、时间范围、染发服务和专长标签，并用结构化发型师资料与 SQLite 排班计算候选；此时不写数据库，也不调用 MCP 或天气。

选择候选后系统请求最终确认。确认时再次检查档期，只有 SQLite 保存成功并产生真实 `appointment_id` 后，才可追加上海预约时段天气。候选生成后若档期被占用，确认必须失败且不调用天气。

对照问题：

```text
冷棕色适合什么肤色？
```

该问题应进入 Consultation 并显示 Citations。LLM 负责理解约束和组织回复；候选排序、价格、时长、排班与预约结果由确定性后端负责。

## Partial Booking Slots / 多轮预约槽位演示

在同一个 session 中依次输入：

```text
预约明天
男士短发
下午两点
```

第一轮只保存日期，继续询问服务和具体时间，不生成默认小时。第二轮由 `SERVICE_CATALOG` 补充男士短发的标准时长与价格，仍不查询发型师、不写 SQLite，也不调用天气。第三轮才将已保存日期与 14:00 组合，进入营业时间、真实排班和冲突校验；保存成功并取得真实 `appointment_id` 后，才可追加上海天气提醒。

第三轮不会自动选择第一位可用发型师。未指定发型师时，精确时间与时间范围都通过 `AvailabilityService` 返回真实候选；用户回复候选序号或姓名后，系统再请求最终确认，确认成功才写入 SQLite。只有明确指定发型师的完整请求才进入现有指定发型师校验与替代推荐流程。

日期与时间范围同样分别保存。例如 `预约明天下午` 后补充 `男士短发`，系统会调用 `AvailabilityService` 搜索下午的真实候选，而不是直接创建 12:00 的预约。LLM 负责理解用户约束；标准时长、价格、候选计算和最终预约结果由确定性后端处理。

可用性查询也可以不包含“预约”二字：

```text
今天下午哪些理发师有空？
男士短发
```

第一轮会保存今天下午的查询范围并询问服务。第二轮虽然单独看只有服务名，但当前 session 的 `availability_search_active` 优先，仍由 AppointmentAgent 查询 SQLite 真实排班，不调用 Consultation 或 MCP Knowledge Service。

## Suggested Questions / 建议演示问题

- `染发前后有什么注意事项？`
- `烫发后多久可以洗头？`
- `临时不能到店，改约规则是什么？`
- `门店几点营业？`
- `男士短发多少钱，需要多久？`
- `明天下午找擅长冷棕色的老师`

## What To Claim / 可以说明

- The application separates LLM/RAG from deterministic booking rules.
- MCP Knowledge Service provides cited retrieval.
- Appointment success, price, duration, schedule, and conflict checks are deterministic.
- FastAPI starts the MCP child process through stdio when MCP is enabled.
- The current evaluation has 28 / 28 functional contracts passing and reports RAG quality separately.
- Optional weather context can enrich a booking success message, but weather failures do not affect booking.

## What Not To Claim / 不要声称

- Do not claim production deployment.
- Do not claim RAG is perfect.
- Do not claim advanced retrieval, memory, orchestration, deployment, or media-processing extensions are implemented in this public release.
- Do not describe the Weather Context Tool as MCP.

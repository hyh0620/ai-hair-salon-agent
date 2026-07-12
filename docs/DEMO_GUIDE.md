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

## Suggested Questions / 建议演示问题

- `染发前后有什么注意事项？`
- `烫发后多久可以洗头？`
- `临时不能到店，改约规则是什么？`
- `门店几点营业？`
- `男士短发多少钱，需要多久？`

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

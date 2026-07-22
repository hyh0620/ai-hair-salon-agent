# 本地运行与深度演示 Runbook

本文面向维护者和需要技术深挖的演示者，用于复现环境、Provider、知识库、认证 Session、故障边界和天气后处理。默认 5 分钟面试流程见[面试演示指南](DEMO_GUIDE.md)，不需要在现场执行本文全部步骤。

## 1. 使用边界

* 使用可控的演示数据，不读取或展示真实用户数据；
* `.env`、Provider Key、JWT Secret、Token、Cookie、SQLite 数据库和知识索引不得提交；
* 普通 pytest 与 CI 必须保持 Hermetic，不调用真实外部 Provider；
* 真实 Provider 验收使用独立、显式流程，不等同于 CI；
* 不通过修改代码、删除预约或手工写数据库来制造演示结果。

## 2. 两个仓库与 Python 环境

主项目和知识服务必须使用相互独立的 `.venv`：

| 仓库 | Python 约束 | 建议本地解释器 |
| --- | --- | --- |
| `ai-hair-salon-agent` | 主要开发、运行和 CI 版本为 Python 3.12 | Python 3.12 |
| `mcp-knowledge-service` | 项目元数据声明支持 Python 3.11+ | 可使用 Python 3.12 |

不要把两个项目安装到同一个虚拟环境，也不要把知识服务描述为只支持 Python 3.12。

## 3. 主项目环境准备

在主项目仓库中执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install \
  -c constraints-py312.txt \
  -r requirements-dev.txt

test -f .env || cp .env.example .env
python -m pip check
```

`constraints-py312.txt` 固定经过验证的 Python 3.12 解析结果；它需要和 `requirements.txt` 或 `requirements-dev.txt` 一起使用。

## 4. MCP Knowledge Service 环境准备

在知识服务仓库中创建独立环境。下面使用 Python 3.12，但知识服务自身声明支持 Python 3.11+：

```bash
python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m pip check
python -m pytest
```

如果已经存在 `.venv`，先确认解释器版本和路径属于知识服务仓库，再决定是否复用。不要把知识服务安装到主项目环境。

## 5. 知识服务 Provider 与配置

从知识服务提供的示例配置创建本地配置，并按其仓库文档设置 Embedding Provider。真实凭据只能保存在私有环境中，不能写入命令、截图、日志或 Git：

```bash
cp config/settings.example.yaml config/settings.yaml
```

配置完成后先运行知识服务自己的测试。不要打印 `settings.yaml` 或私有环境文件来证明配置成功。

## 6. 导入受控知识语料

首次准备 `salon_knowledge`，或明确需要重建受控演示索引时，在知识服务仓库执行：

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

Ingest 会写入知识服务自己的 ChromaDB 和 BM25 目录。正常查询和默认面试演示不应重新 Ingest，也不应修改或提交这些运行时数据。

## 7. 主项目 MCP 配置

主项目通过以下私有配置定位知识服务：

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<MCP_REPOSITORY>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<MCP_REPOSITORY>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

`RAG_MCP_SERVER_PYTHON` 必须指向知识服务自己的虚拟环境。公开 `.env.example` 默认 `RAG_MCP_ENABLED=false`，所以 Booking 和本地页面可以先独立运行。

## 8. FastAPI 与 MCP stdio 生命周期

完成 MCP 路径、模块、工作目录和 Collection 配置后，只需启动 FastAPI：

```bash
source .venv/bin/activate
python -m uvicorn app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --no-proxy-headers
```

FastAPI lifespan 会自动：

1. 创建 `MCPKnowledgeGateway`；
2. 通过 Official MCP Python SDK 启动 stdio 子进程；
3. 执行 `initialize` 和 `list_tools`；
4. 验证 `query_knowledge_hub`；
5. 在 Consultation 请求之间复用 Session；
6. 应用关闭时清理 Session 和子进程。

正常集成不要求先手动常驻启动 MCP Server。手动运行 `python -m src.mcp_server.server` 仅用于单独验证 stdio 协议；终端没有交互提示并不代表服务失败。

## 9. 服务状态检查

启动后检查：

```bash
curl http://127.0.0.1:8000/health
```

浏览器入口：

* 首页：`http://127.0.0.1:8000`
* 系统状态：`http://127.0.0.1:8000/status`
* Swagger：`http://127.0.0.1:8000/docs`
* 排班：`http://127.0.0.1:8000/stylist-schedule`

状态页面适合确认配置，不应展示 Provider Key、Token 或数据库路径。

## 10. Booking 演示准备

使用临时或专用演示 SQLite，不要删除现有预约来制造空档。现场候选必须动态获取：

1. 在聊天输入“明天下午找擅长冷棕色的老师”；
2. 使用系统实际返回的未来候选；
3. 回复候选序号或“姓名 + 时间”；
4. 检查摘要后回复“确认”；
5. 记录页面返回的真实 `appointment_id`；
6. 在排班页面按实际日期确认对应 `busy` 时段。

如果没有候选，改用系统查询得到的其他未来日期或重建临时演示数据库。不要假设某个固定日期、发型师 ID 或人员永远可用。

冲突演示应复用刚刚成功创建的同一发型师和时间，并使用另一个测试 owner 发起请求。预期第二次写入被拒绝；不要硬编码未来会过期的时间。

## 11. RAG 与 Citations 演示准备

确认 `/health` 中知识服务可用后，在聊天或 Consultation API 提问：

```text
冷棕色适合什么肤色？
```

检查：

* 路由进入 Consultation；
* MCP 调用 `query_knowledge_hub`；
* 返回内容非空并带 Citations；
* 来源属于受控知识库；
* 没有写预约或排班。

MCP 是调用协议，Dense Retrieval、BM25 和 RRF 是知识服务内部的 RAG 链路。RAG 不决定价格、排班、冲突或预约成功。

## 12. 认证 Session 深度演示

认证演示优先使用隔离入口：

```bash
bash scripts/run_isolated_validation.sh
```

该脚本使用临时 SQLite 和测试认证配置，并拒绝真实外部 Provider。可以展示：

* 注册或登录后获得服务器验证的账户 owner；
* `/api/auth/me` 返回当前账户的安全显示信息；
* 账户与游客预约范围相互隔离；
* Auth Session 与 `chat_session_id` 分别管理凭据状态和短期对话状态。

不要在终端或浏览器开发工具中展示 Token、Cookie、Hash、JWT Secret、密码或原始邮箱。

## 13. Refresh、Logout、重放和多 Session

这些流程不属于默认 5 分钟演示。深度验证应在隔离环境中完成，并优先引用自动化测试结果：

1. Access 过期后，浏览器 single-flight Refresh 最多重试原请求一次；
2. Refresh Token 轮换后，Grace Window 内的并发重复返回 409；
3. Grace Window 外的重放撤销当前 Auth Session；
4. Logout 撤销当前 Session，已复制的 Bearer Token 随即失效；
5. 同一账户的另一个 Auth Session 保持可用。

对应回归位于 `tests/test_auth_refresh_api.py`、`tests/test_auth_refresh_frontend.py` 和 `tests/test_auth_sessions.py`。手工验证时只记录 HTTP 状态和业务结果，不复制凭据原值。

## 14. MCP 故障注入

故障注入使用独立进程、临时数据库和临时错误工作目录，不修改正常 `.env`。可将单独验收实例的 `RAG_MCP_SERVER_CWD` 指向不存在的临时目录，再启动 FastAPI。

验证：

* Consultation 返回 HTTP 503 和稳定原因 `mcp_rag_unavailable`；
* Booking 仍可查询候选并完成确定性流程；
* 故障实例退出后删除临时目录和数据库；
* 不把故障配置带回正常演示环境。

仓库中的 `eval/mcp_runtime_failure_e2e.py` 用于验证该契约。不要把 pytest Mock 结果描述为真实 MCP 集成结果。

## 15. Open-Meteo 验证

公开示例配置默认：

```env
WEATHER_ENABLED=true
WEATHER_PROVIDER=open_meteo
WEATHER_LOCATION_NAME=上海
```

Open-Meteo 不需要 API Key。只有聊天预约完成事务提交并获得真实 `appointment_id` 后才查询预约时段天气；搜索候选、等待确认、冲突、取消和 REST 预约接口不会追加天气。

天气失败时预约仍保持成功。普通 pytest 和 CI 设置 `EXTERNAL_CALL_POLICY=deny`，因此不会调用真实 Open-Meteo。

## 16. 临时 SQLite 与数据保护

默认验证使用临时 SQLite。可以复用 `scripts/run_isolated_validation.sh`，或为独立进程设置仅指向临时目录的 `DATABASE_URL`。

验证前后确认：

* 没有读取或修改真实业务数据库；
* 临时数据库和目录在进程退出后删除；
* 不提交 `data/`、日志、trace 或本地报告；
* 不手工插入记录来伪造成功结果。

## 17. Hermetic 验证

在主项目开发环境执行：

```bash
python -m pip check
bash scripts/check_release_readiness.sh
bash scripts/test_hermetic.sh
python -m compileall agents api services db config eval web tests
git diff --check
```

Hermetic 测试在应用导入前设置 `EXTERNAL_CALL_POLICY=deny`，使用临时 SQLite、Fake/Mock LLM、Mock MCP、Mock Weather 和固定时间。它不读取真实 `.env`，也不等同于真实 Provider 验收。

## 18. 安全与清理

演示结束后：

1. 停止 FastAPI 和所有隔离验收进程；
2. 确认 lifespan 已清理 MCP stdio 子进程；
3. 删除临时 SQLite、临时目录和本地报告；
4. 保留真实 `.env`、知识索引和业务数据库原状；
5. 检查 Git 工作区没有运行时数据；
6. 不在截图、录屏或终端历史中保留凭据。

## 19. 常见故障

### Booking 可用但 Consultation 返回 503

检查 `RAG_MCP_ENABLED`、知识服务解释器、模块、工作目录和 Collection。正常集成由 FastAPI lifespan 拉起子进程，不需要另开终端常驻启动 MCP。

### Consultation 可用但没有 Citations

确认已导入目标 Collection、查询调用的是 `query_knowledge_hub`，并检查知识服务自身的 Dense、BM25 和 RRF 配置。不要用手工拼接来源替代 Citations。

### 没有可用候选

查询其他未来日期或重建临时演示数据库。不要删除真实预约，不要硬编码另一个日期或发型师 ID。

### 天气没有显示

先确认聊天预约已经成功提交并返回 `appointment_id`。天气服务不可用时省略提醒是预期降级，不应重试预约或修改数据库。

### 认证演示失败

确认使用隔离测试配置和临时数据库。不要打印私有环境文件排查；使用安全状态、HTTP 状态码和自动化测试定位问题。

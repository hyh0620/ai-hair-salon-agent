# MCP 知识服务集成

AI Hair Salon Agent 将非结构化知识咨询委托给独立的 [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)。

职责关系：

* 主项目是 MCP 客户端（Client）；
* 独立知识服务是 MCP 服务端（Server）；
* MCP 负责标准化跨进程工具调用；
* RAG 是知识服务内部的检索、融合和引用链路。

MCP 与 RAG 不是同一个组件。主项目不直接维护 ChromaDB 或 BM25 索引，它们位于独立知识服务中。

## 环境变量

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

只有当 `RAG_MCP_SERVER_PYTHON` 和 `RAG_MCP_SERVER_CWD` 指向有效的独立知识服务目录后，才启用 `RAG_MCP_ENABLED=true`。

默认 `.env.example` 使用 `RAG_MCP_ENABLED=false`，因此预约功能和本地页面可以在没有知识服务的情况下启动。

## FastAPI 生命周期（lifespan）

FastAPI 启动期间：

1. `MCPKnowledgeGateway` 读取环境配置；
2. 使用官方 MCP Python SDK 的 stdio 传输启动知识服务子进程；
3. 创建 `ClientSession`；
4. 调用 `initialize`；
5. 调用 `list_tools`；
6. 确认 `query_knowledge_hub` 可用；
7. 在后续知识咨询请求之间复用 MCP 会话。

FastAPI 关闭时会清理 MCP 会话和子进程。手动执行 `python -m src.mcp_server.server` 只适用于单独验证 MCP 服务端；正常应用运行由主项目应用生命周期管理。

## 查询链路

```text
POST /api/consultation/query
  ↓
ConsultantAgent
  ↓
MCPKnowledgeGateway.query_knowledge
  ↓
ClientSession.call_tool("query_knowledge_hub")
  ↓
独立 MCP Knowledge Service
  ↓
向量检索 + BM25
  ↓
RRF
  ↓
回答 + sources + 引用来源
```

`query_knowledge_hub` 是主项目实际调用的 MCP 工具名称，不应翻译或改名。

## 知识服务内部检索

RAG 在独立服务内部执行：

* 向量检索提供语义召回；
* BM25 提供关键词匹配；
* Reciprocal Rank Fusion（RRF）融合两路排名；
* 引用来源（Citations）返回来源信息。

知识服务适合回答护理知识、门店信息、服务政策和发型建议。当前使用小型受控语料，检索指标只作为可复现回归证据。

## 业务规则边界

知识服务不决定：

* 标准价格；
* 标准时长；
* 发型师是否存在或支持服务；
* 真实排班；
* 时间冲突；
* 预约写入；
* 预约是否成功。

这些结果由主项目中的 `SERVICE_CATALOG`、`AvailabilityService`、`AppointmentService` 和 SQLite 决定。

ChromaDB 是独立知识服务的向量存储，不是预约数据库；主项目的预约事实保存在 SQLite。

## 错误契约

如果 MCP Knowledge Service 在启动或运行期间不可用，知识咨询返回 HTTP 503 和稳定原因 `mcp_rag_unavailable`，而不是伪造知识结果。

预约链路不依赖 MCP Knowledge Service，因此预约、档期查询和预约生命周期接口保持可用。天气也不是 MCP 工具，不经过该知识服务。

## 本地验证

完整准备步骤见[本地运行与深度演示手册](DEMO_RUNBOOK.md)。启用知识服务后，可以检查：

* `/health` 中的知识服务状态；
* `/api/consultation/query` 的 `sources` 与引用来源；
* MCP 不可用时的 HTTP 503；
* 同一故障下预约功能仍可执行。

不要把手动启动 stdio 服务端后终端没有交互提示误判为失败；stdio 服务端需要 MCP 客户端发送 `initialize`、`list_tools` 和工具调用。

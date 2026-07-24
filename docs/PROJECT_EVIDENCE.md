# 项目证据索引

本文用于核验项目能力与简历表述。代码位置和验证命令指向当前仓库事实；“当前限制”用于区分已实现能力与生产化规划。

## 项目定位

> Agent 负责理解，确定性业务服务负责决策，SQLite 负责业务事实，MCP Knowledge Service 负责知识检索。

自然语言层识别意图、补全槽位并维护多轮状态。价格、时长、发型师能力、排班、冲突和预约写入由确定性服务处理。知识咨询通过独立 MCP 服务获取检索结果和引用来源，不参与预约事实判断。

## 可核验证据

| 简历可用表述 | 代码或文档位置 | 验证命令 | 当前限制 |
| --- | --- | --- | --- |
| 使用 `TaskClassificationAgent`、`AppointmentAgent` 和 `ConsultantAgent` 分离任务路由、预约和知识咨询 | `agents/task_classification_agent.py`、`agents/appointment_agent.py`、`agents/consultant_agent.py` | `python -m pytest tests/test_chat_sessions_and_routing.py` | 这是单体应用内的 Agent 组件，不是分布式多 Agent 平台 |
| 支持多轮 Slot Filling、候选选择和会话内状态恢复 | `agents/appointment/appointment_processor.py`、`agents/appointment/lifecycle_processor.py`、`api/chat_handler.py` | `python -m pytest tests/test_partial_booking_slots.py tests/test_appointment_lifecycle_chat.py` | 对话会话保存在进程内存，未使用 Redis 共享 |
| 标准服务、价格和时长由 `SERVICE_CATALOG` 统一提供 | `services/service_catalog.py` | `python -m pytest tests/test_service_catalog.py` | 目录是本地结构化配置，不是门店员工管理后台 |
| `AvailabilityService` 按服务能力、专长和 SQLite 排班生成真实候选 | `services/availability_service.py` | `python -m pytest tests/test_availability_search.py` | 当前面向单门店、本地 SQLite |
| `AppointmentService` 统一处理创建、查询、取消、修改和改期 | `services/appointment_service.py`、`api/appointment.py` | `python -m pytest tests/test_appointment_lifecycle_service.py tests/test_appointment_lifecycle_api.py` | 未实现支付、自动完成状态或取消费用政策 |
| 预约写入使用 SQLite `BEGIN IMMEDIATE` 原子事务 | `db/base/session_manager.py`、`services/appointment_service.py` | `python -m pytest tests/test_atomic_booking.py` | SQLite 事务不等同于跨服务分布式事务 |
| INSERT/UPDATE Trigger 阻止同一发型师重叠的 `busy` 排班 | `db/base/session_manager.py` | `python -m pytest tests/test_atomic_booking.py` | Trigger 仅保护当前 SQLite 数据库 |
| 取消和修改使用 `version` / `expected_version` 乐观并发控制 | `db/models.py`、`services/appointment_service.py`、`api/core/response_models.py` | `python -m pytest tests/test_appointment_lifecycle_service.py` | 不提供跨数据库的全局版本协调 |
| REST API 与聊天流程共用预约生命周期业务服务 | `api/appointment.py`、`agents/appointment/lifecycle_processor.py` | `python -m pytest tests/test_appointment_lifecycle_api.py tests/test_appointment_lifecycle_chat.py` | 自然语言解析覆盖受测试约束，不声称理解所有中文表达 |
| Access JWT 包含 `sid`，服务端认证会话可吊销 | `services/auth_service.py`、`services/auth_session_service.py`、`api/auth.py` | `python -m pytest tests/test_auth_sessions.py tests/test_auth_refresh_api.py` | 当前认证适合项目演示，未提供企业级身份治理 |
| Refresh Token 使用一次性轮换并只持久化 SHA-256 哈希 | `services/auth_session_service.py`、`db/models.py` | `python -m pytest tests/test_auth_sessions.py` | 仍是单应用、本地数据库实现 |
| 登录账户与游客预约归属隔离 | `api/auth_dependencies.py`、`api/chat_handler.py` | `python -m pytest tests/test_chat_owner_identity.py tests/test_auth_api_and_identity.py` | 游客标识不是生产级身份认证 |
| `MCPKnowledgeGateway` 使用 MCP 客户端调用独立知识服务 | `services/mcp_knowledge_gateway.py`、`docs/RAG_SERVICE_INTEGRATION.md` | `python -m pytest tests/test_mcp_knowledge_gateway.py` | 默认自动化测试使用模拟 MCP；真实服务需按运行手册配置 |
| 知识服务内部采用 Dense Retrieval、BM25 和 RRF，并返回 Citations | `docs/RAG_SERVICE_INTEGRATION.md`、关联仓库 `mcp-knowledge-service` | 按 `docs/DEMO_RUNBOOK.md` 运行真实 Provider 验收 | 主仓库不重复实现第二套检索引擎 |
| MCP 故障与预约链路隔离 | `services/mcp_knowledge_gateway.py`、`eval/mcp_runtime_failure_e2e.py` | `python -m pytest tests/test_mcp_knowledge_gateway.py tests/test_rag_regression.py` | 真实故障 E2E 需要单独启动故障模式服务 |
| 自动化回归覆盖事务、并发、认证、路由、预约和知识网关 | `tests/`、`scripts/test_hermetic.sh` | `bash scripts/test_hermetic.sh` | pytest 结果是工程回归证据，不是生产准确率 |
| 28 条 Golden Dataset 将功能契约与检索质量分开统计 | `eval/golden_dataset.jsonl`、`eval/run_evaluation.py` | `python -m pytest tests/test_evaluation_contracts.py tests/test_evaluation_reproducibility.py` | 完整评估需要真实 LLM、Embedding、MCP 和故障模式服务 |
| 检索质量使用来源级 Hit@1、Hit@3 和 MRR | `eval/evaluation_metrics.py`、`eval/verified_snapshot.py` | `python eval/verify_snapshot.py <snapshot.json>` | 指标只适用于对应语料、数据集和快照，不代表通用准确率 |

## 证据边界

当前仓库没有实现或验证 Redis、PostgreSQL、Ragas、Cross-Encoder Reranker、多实例生产部署、商业上线或真实用户流量。相关方向仅记录在 [项目路线图](ROADMAP.md)，不得作为现有能力表述。

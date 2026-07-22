# AI Hair Salon Agent v1.0.0

## Overview

AI Hair Salon Agent 是一个自然语言驱动的理发店预约与知识咨询系统。Agent 层理解模糊表达、补全预约槽位并维护多轮状态，确定性业务服务负责价格、标准时长、发型师能力、排班、冲突和预约写入。SQLite 保存预约、排班、账户和认证 Session 等业务事实。独立 MCP Knowledge Service 提供带 Citations 的门店知识与护理咨询。

## Highlights

- 自然语言意图识别：`create_booking`、`search_availability`、预约生命周期和 `consultation`。
- 多轮 Slot Filling，分别保存日期、精确时间、时间范围、服务、发型师和专长偏好。
- 相对日期、精确时间和上午/下午/晚上等模糊时间范围解析。
- 基于 `SERVICE_CATALOG` 的服务、标准价格和标准时长。
- 结构化服务能力与发型师专长映射。
- `AvailabilityService` 基于 SQLite 真实排班生成确定性候选。
- 未指定发型师时先选择候选，再进行最终确认。
- 创建、查询、修改、改期和取消预约。
- `BEGIN IMMEDIATE`、SQLite Trigger、二次冲突检查和乐观并发 `version`。
- MCP stdio 调用与 Dense Retrieval、BM25、RRF、Citations。
- 预约保存成功后的 Open-Meteo 上海天气提醒，失败不影响预约结果。
- 账户与游客预约空间隔离。
- Argon2 密码 Hash 与受信账户 Owner。
- 登录、注册和 Refresh 的有界进程内限流。
- 带内部 `sid` 的短期 Access JWT。
- 仅保存 SHA-256 Hash 的不透明 Refresh Token、单次轮换与重放检测。
- SQLite 持久化服务端 Auth Session 和当前 Session Logout 吊销。
- Hermetic 测试、外部 Provider 调用隔离和 Python 3.12 GitHub Actions。

## Engineering Boundaries

- LLM 负责理解用户表达和组织回复，不决定价格、时长、排班、冲突或预约成功。
- MCP 是跨进程工具调用协议，RAG 是独立知识服务内部的检索、融合和引用链路；二者不是同一组件。
- MCP/RAG 只处理知识咨询，不参与真实预约事实判断。
- 天气是预约成功后的可选上下文，不参与预约提交事务。
- SQLite 的事务和 Trigger 提供单应用实例内的一致性保护，不是分布式事务。
- 认证限流保存在当前应用进程内，不跨 Worker 或实例共享。
- Chat Session 管理短期对话状态，不是长期 Memory。
- Auth Session 管理登录凭据的有效性，与 Chat Session 不同。
- 游客 `anonymous_owner_id` 是可伪造的业务范围，不是安全认证。
- 当前版本是工程演示和架构验证项目，不代表已经生产部署或拥有真实商业流量。

## Validation

| Check | Result |
| --- | ---: |
| Python | 3.12 |
| pytest | 403 passed |
| Failures | 0 |
| Warnings | 0 |
| Required Check | Python 3.12 |
| Functional Contract | 28 / 28 |
| RAG Cases | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| Citation expected-source match | 11 / 11 |

Functional Contract 与 RAG 检索质量分别评估。RAG 数字来自 7 份源文档、24 个语义切片组成的小型受控语料，是仓库中保存的 Verified Evaluation Snapshot，不代表生产准确率或通用 Benchmark。

## 真实 Provider 验收摘要

- OpenAI-compatible Qwen `qwen-plus` 调用成功。
- MCP stdio 初始化和 `query_knowledge_hub` 调用成功。
- Consultation 返回 4 个 Citations，并命中预期知识源。
- Open-Meteo 在预约事务 commit 并生成真实 `appointment_id` 后调用成功。
- MCP 故障时 Consultation 明确降级，Booking 保持可用。
- Chroma 查询前后 Collection ID、24 个 Chunk、7 个 Document、24 个 Embedding、Metadata 和 BM25 逻辑内容不变。
- Chroma SQLite/HNSW 文件变化属于 expected runtime storage writes，不代表业务数据修改。

该验收使用显式允许外部调用的隔离流程，不属于 Hermetic CI；CI 继续拒绝真实外部 Provider。

## Upgrade Note

- 旧版本中不含 `sid` 的 Access Token 在升级后失效，用户需要重新登录。
- 不迁移或提交本地 `.env`、SQLite 数据库、Provider Secret、日志或索引数据。
- v1.0.0 Tag 应指向通过发布门禁、Hermetic CI 和真实 Provider 验收的最终 `main` Commit。

## Known Limitations

- 面向单应用实例运行。
- 使用本地 SQLite，不是服务型分布式数据库。
- Chat Session 保存在应用进程内。
- 认证限流保存在应用进程内。
- 没有 Redis 或 PostgreSQL。
- 没有 MFA、OAuth 或 RBAC。
- 没有邮箱验证和密码重置。
- 没有登录设备管理 UI 或退出全部设备。
- 没有真实支付系统。
- 没有生产级监控、告警和审计基础设施。
- 没有真实商业流量验证。

## Quick Start

```bash
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

公开示例配置不包含真实 LLM 或 Embedding 凭据，RAG MCP 默认关闭，因此 Booking 与本地页面可以先独立启动；启用真实 Provider 需要在私有 `.env` 和独立 MCP Knowledge Service 中完成本地配置。账户功能需要在私有 `.env` 中设置符合要求的 JWT Secret，但不得把该文件提交到仓库。

## Demo

参见 [5 分钟面试演示](DEMO_GUIDE.md) 和 [本地运行与深度演示 Runbook](DEMO_RUNBOOK.md)。

## Related Repository

- [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)

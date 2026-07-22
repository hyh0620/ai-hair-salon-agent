# AI Hair Salon Agent v1.0.0

## 项目概述

AI Hair Salon Agent 是一个自然语言驱动的理发店预约与知识咨询系统。Agent 层理解模糊表达、补全预约槽位并维护多轮状态，确定性业务服务负责价格、标准时长、发型师能力、排班、冲突和预约写入。SQLite 保存预约、排班、账户和认证会话等业务事实。独立 MCP Knowledge Service 提供带引用来源的门店知识与护理咨询。

## 核心能力

- 自然语言意图识别：`create_booking`、`search_availability`、预约生命周期和 `consultation`。
- 多轮槽位补全（Slot Filling），分别保存日期、精确时间、时间范围、服务、发型师和专长偏好。
- 相对日期、精确时间和上午/下午/晚上等模糊时间范围解析。
- 基于 `SERVICE_CATALOG` 的服务、标准价格和标准时长。
- 结构化服务能力与发型师专长映射。
- `AvailabilityService` 基于 SQLite 真实排班生成确定性候选。
- 未指定发型师时先选择候选，再进行最终确认。
- 创建、查询、修改、改期和取消预约。
- `BEGIN IMMEDIATE`、SQLite 触发器、二次冲突检查和乐观并发 `version`。
- MCP stdio 调用与向量检索、BM25、RRF、引用来源。
- 预约保存成功后的 Open-Meteo 上海天气提醒，失败不影响预约结果。
- 账户与游客预约空间隔离。
- Argon2 密码哈希与可信账户预约归属。
- 登录、注册和凭据刷新的有界进程内限流。
- 带内部 `sid` 的短期 Access JWT。
- 仅保存 SHA-256 哈希的不透明刷新令牌、单次轮换与重放检测。
- SQLite 持久化服务端认证会话和当前会话退出登录吊销。
- 隔离式（Hermetic）测试、外部服务调用隔离和 Python 3.12 GitHub Actions。

## 工程边界

- LLM 负责理解用户表达和组织回复，不决定价格、时长、排班、冲突或预约成功。
- MCP 是跨进程工具调用协议，RAG 是独立知识服务内部的检索、融合和引用链路；二者不是同一组件。
- MCP/RAG 只处理知识咨询，不参与真实预约事实判断。
- 天气是预约成功后的可选上下文，不参与预约提交事务。
- SQLite 的事务和触发器提供单应用实例内的一致性保护，不是分布式事务。
- 认证限流保存在当前应用进程内，不跨工作进程或实例共享。
- 对话会话管理短期对话状态，不是长期记忆。
- 认证会话管理登录凭据的有效性，与对话会话不同。
- 游客 `anonymous_owner_id` 是可伪造的业务范围，不是安全认证。
- 当前版本是工程演示和架构验证项目，不代表已经生产部署或拥有真实商业流量。

## 验证结果

| 检查项 | 结果 |
| --- | ---: |
| Python | 3.12 |
| pytest | 403 passed |
| 失败 | 0 |
| 警告 | 0 |
| 必需检查项 | Python 3.12 |
| 功能契约 | 28 / 28 |
| RAG 用例 | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| 引用来源与预期来源匹配 | 11 / 11 |

功能契约与 RAG 检索质量分别评估。RAG 数字来自 7 份源文档、24 个语义切片组成的小型受控语料，是仓库中保存的已验证评估快照，不代表生产准确率或通用基准测试。

## 真实外部服务验收

- OpenAI-compatible Qwen `qwen-plus` 调用成功。
- MCP stdio 初始化和 `query_knowledge_hub` 调用成功。
- 知识咨询返回 4 个引用来源，并命中预期知识源。
- Open-Meteo 在预约事务提交并生成真实 `appointment_id` 后调用成功。
- MCP 故障时知识咨询明确降级，预约功能保持可用。
- Chroma 查询前后知识集合 ID、24 个语义切片、7 份文档、24 个向量、元数据和 BM25 逻辑内容不变。
- Chroma SQLite/HNSW 文件变化属于预期的运行时持久化写入（expected runtime storage writes），不代表业务数据修改。

该验收使用显式允许外部调用的隔离流程，不属于隔离式 CI；CI 继续拒绝真实外部服务。

## 升级说明

- 旧版本中不含 `sid` 的访问令牌在升级后失效，用户需要重新登录。
- 不迁移或提交本地 `.env`、SQLite 数据库、外部服务密钥、日志或索引数据。
- v1.0.0 版本标签应指向通过发布门禁、隔离式 CI 和真实外部服务验收的最终 `main` 提交。

## 已知限制

- 面向单应用实例运行。
- 使用本地 SQLite，不是服务型分布式数据库。
- 对话会话保存在应用进程内。
- 认证限流保存在应用进程内。
- 没有 Redis 或 PostgreSQL。
- 没有 MFA、OAuth 或 RBAC。
- 没有邮箱验证和密码重置。
- 没有登录设备管理 UI 或退出全部设备。
- 没有真实支付系统。
- 没有生产级监控、告警和审计基础设施。
- 没有真实商业流量验证。

## 快速开始

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

公开示例配置不包含真实 LLM 或 Embedding 凭据，RAG MCP 默认关闭，因此预约功能与本地页面可以先独立启动；启用真实外部服务需要在私有 `.env` 和独立 MCP Knowledge Service 中完成本地配置。账户功能需要在私有 `.env` 中设置符合要求的 JWT 密钥，但不得把该文件提交到仓库。

## 演示材料

参见：

- [5 分钟项目演示](https://github.com/hyh0620/ai-hair-salon-agent/blob/v1.0.0/docs/DEMO_GUIDE.md)
- [本地运行与深度演示手册](https://github.com/hyh0620/ai-hair-salon-agent/blob/v1.0.0/docs/DEMO_RUNBOOK.md)

## 相关仓库

- [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)

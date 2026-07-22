# 系统架构

AI Hair Salon Agent 将自然语言交互、确定性预约服务和独立知识咨询服务分为不同责任层。

> **Agent 负责理解，业务服务负责决策，SQLite 负责保存事实。**

主架构图见 [`../architecture.svg`](../architecture.svg)。

## 请求入口与中心路由

Web 页面和 REST API 统一进入 FastAPI。聊天请求由中心路由结合以下信息决定实际业务链路：

1. 当前会话是否存在活动中的预约流程；
2. 高精度业务预路由是否识别到创建、档期查询或生命周期操作；
3. 确定性日期、时间和选择表达；
4. LLM 对开放表达的辅助分类。

后端拥有最终路由决定权。即使前端提交了错误路由，只要当前消息或会话状态明确属于预约，后端仍会进入预约链路。

系统不是把所有意图都交给 LLM：明确状态和高精度规则优先，LLM 用于更开放的分类、槽位提取和回复组织。

## 可选账户认证与 RequestIdentity

账户认证位于 HTTP 边界，不进入 Agent、LLM、MCP 或预约业务服务：

```text
Bearer 访问 JWT / HttpOnly 访问与刷新 Cookie / anonymous_owner_id
  ↓
AuthService + AuthSessionService
  ↓
固定 HS256 声明字段 + JWT sid + 有效认证会话 + 有效用户
  ↓
AuthSessionRepository
  ├── auth_sessions
  └── auth_refresh_tokens（仅保存 SHA-256 哈希）
  ↓
RequestIdentity
  ├── authenticated: account:<JWT sub>
  └── anonymous: 校验后的游客预约归属
  ↓
聊天路由 / 预约 REST API / 用户行为 API
  ↓
Agent 只接收已解析 owner_id
```

密码仅以 Argon2 哈希保存在 `users` 表。短期访问 JWT 包含内部认证会话 UUID `sid`，每次请求都会检查会话所属用户、撤销状态和绝对到期时间；缺少 `sid` 的旧 JWT 不被接受。浏览器访问令牌位于 HttpOnly Cookie，JavaScript 只读取独立的 CSRF Cookie 并通过 `X-CSRF-Token` 提交；Swagger、CLI 和测试可使用 Bearer 令牌，Bearer 请求不需要 Cookie CSRF。如果 Bearer 与 Cookie 同时存在但对应不同账户，请求直接返回 401。

刷新令牌（Refresh Token）是至少 256 bit 随机强度的不透明值，只存在于路径为 `/api/auth` 的 HttpOnly Cookie；SQLite 仅保存 SHA-256 哈希。刷新在单个 `BEGIN IMMEDIATE` 事务内标记旧令牌 `used_at`、创建新哈希、关联 `replaced_by_token_id` 并更新认证会话。3 秒宽限窗口内的重复返回 409；窗口外重放撤销该会话及其未使用刷新令牌。认证会话的绝对寿命不会因轮换延长。

浏览器普通同源请求遇到认证阶段 401 时通过共享 Promise 执行一次凭据刷新，轮换 CSRF 后最多重试原请求一次。退出登录（Logout）可由有效 Bearer `sid` 定位认证会话，或在访问令牌过期时由刷新 Cookie 定位会话；它只撤销当前登录会话，并清除访问、刷新与 CSRF Cookie。

登录、注册和刷新在进入昂贵或持久化认证操作前经过有界、线程安全的进程内滑动窗口限流。登录按直连客户端以及“直连客户端 + 规范化邮箱”两个范围限制，注册和刷新按直连客户端限制；429 响应带 `Retry-After`。客户端地址只读取 `request.client.host`，不信任代理转发头。该状态在进程重启后清空且不跨工作进程（Worker）或实例共享，生产部署需要可信代理、API 网关或 Redis 等共享限流层。

认证身份优先于客户端字段：登录后，REST 的 `user_id` 与聊天请求的 `owner_id` 都不能覆盖 JWT 身份。无效或过期令牌返回 401，不会降级为游客；未登录调用者不能进入 `account:` 预约归属命名空间。认证未启用或密钥不合法不会阻断游客预约、知识咨询、SQLite、MCP/RAG 或天气，但认证 API 返回 503。

用户行为分析和回访提醒同样在 API 边界解析 `RequestIdentity`，然后只把可信 `owner_id` 传给 Agent、业务服务和数据仓库。账户行为使用 `account:<JWT sub>`，游客行为使用通过格式校验的 `X-Anonymous-Owner-ID`；客户端请求体或查询参数不能切换账户范围。无效令牌不会回退到游客，分析响应只包含安全的当前查看者显示信息，不暴露内部预约归属。

预约成功提交后才记录对应预约归属的行为。`chat_session_id` 继续只用于对话追踪，不能替代账户行为归属。游客历史不会自动归入后续登录账户，退出后仍可回到原游客范围。

## 会话与预约归属边界

`ChatSessionRegistry` 为每个浏览器对话会话（Chat Session）保存独立的：

* `TaskClassificationAgent`；
* `AppointmentAgent`；
* `ConsultantAgent`；
* 对话历史和预约历史；
* 活动预约槽位；
* 候选列表与待确认状态；
* 生命周期操作上下文。

同一对话会话的请求通过 `asyncio.Lock` 串行处理。注册表设置 3600 秒 TTL 和最多 100 个会话，重置时删除对应进程内状态。

预约所有权使用独立的 `anonymous_owner_id`：

* `ChatSessionRegistry` 始终只使用 `chat_session_id` 作为键；
* 创建、查询、修改和取消预约时，将 `anonymous_owner_id` 传给 `AppointmentService`；
* 清空聊天或会话 TTL 过期只丢弃对话状态，不改变 SQLite 中预约的归属；
* 浏览器保留匿名归属标识后，新对话会话仍可管理原预约；
* 旧调用方缺少 `owner_id` 时，后端暂时回退到规范化后的会话 ID 并记录弃用日志。

这是游客兼容路径。登录账户改用 `account:<user_uuid>`，该值只由后端根据已验证 JWT 生成。认证会话（Auth Session）的 `sid` 只管理凭据吊销，不参与预约归属。注册、登录和退出轮换 `chat_session_id`，凭据刷新不轮换；浏览器继续保留原 `anonymous_owner_id`，退出后可回到游客空间。系统不会把游客预约自动迁移到账户。

这些机制用于隔离业务对话上下文，不是安全鉴权：

* 会话 ID 不是登录凭证；
* 状态只保存在当前应用进程内；
* 应用重启后状态不会保留；
* 多实例之间不会自动共享状态；
* 它不是持久化用户记忆，也不是 Redis 分布式会话。

> 认证会话管理可吊销登录凭据，`chat_session_id` 管理短期交互状态，`anonymous_owner_id` 管理可伪造的游客业务范围；三者互不替代。

## Agent 职责分工

项目采用中心路由协调、按职责拆分的 Agent 组件：

### `TaskClassificationAgent`

* 结合规则、会话状态和模型分类任务；
* 在预约链路与知识咨询链路之间分流；
* 不执行预约写入。

### `AppointmentAgent`

* 解析预约意图和槽位；
* 维护创建、查询、取消和修改的多轮状态；
* 理解候选选择与最终确认；
* 调用确定性服务，不直接修改数据库。

### `ConsultantAgent`

* 将知识问题交给 `MCPKnowledgeGateway`；
* 组织知识服务返回的回答和引用来源；
* 不提供真实排班或预约裁决。

这些组件在同一应用进程中由中心路由和共享会话协调，不是独立部署、自由协商或自主规划的分布式 Agent。

## 确定性预约链路

预约链路保持以下调用关系：

```text
AppointmentAgent
  ↓
规则解析 + LLM 辅助槽位提取
  ↓
SERVICE_CATALOG
  ↓
AvailabilityService
  ↓
候选选择与最终确认
  ↓
AppointmentService
  ↓
SQLite
```

### `SERVICE_CATALOG`

保存标准服务、价格和时长，是这些业务事实的权威来源。客户端或 LLM 提供的价格、结束时间和标准时长不会覆盖目录。

### `AvailabilityService`

只读取结构化业务数据和 SQLite 排班，负责：

* 服务支持过滤；
* 专长匹配；
* 营业时间约束；
* 完整服务时长计算；
* 精确时间或时间范围候选生成；
* 已有 `busy` 排班冲突过滤；
* 过去时间过滤；
* 稳定候选排序。

未指定发型师时，无论请求是精确时间还是时间范围，都先返回候选。即使只有一位候选，也必须由用户选择并最终确认，系统不会自动写入。

### `AppointmentService`

统一承载聊天和 REST API 的预约规则，包括时间、营业时间、服务能力、状态、预约归属范围、版本、冲突与事务。API 层和 Agent 层不复制这些写入规则。

## 预约生命周期

系统支持创建、查询、取消和修改预约。

### 创建

```text
结构化槽位
→ 查询真实候选
→ 用户选择
→ 最终确认
→ 二次冲突检查
→ 原子写入
→ appointment_id
```

### 查询

查询以 `appointment_id + owner_id` 或预约归属范围为条件，返回数据库中的最新状态和 `version`。会话保存的候选只是交互上下文，不是权威事实。

### 取消

取消不删除记录，而是在同一事务中将 `appointments` 和对应 `stylist_schedules` 更新为 `cancelled`，从而保留关系并释放档期。

### 修改或改期

PATCH 字段与数据库中的当前预约合并。服务变化时重新从 `SERVICE_CATALOG` 计算价格、时长和结束时间；目标档期检查排除预约自身；成功后保持 `appointment_id` 不变并递增 `version`。

## SQLite 事务与并发边界

创建、取消和修改都在一个 SQLAlchemy 数据库会话和同一 SQLite 连接中执行 `BEGIN IMMEDIATE`：

```text
BEGIN IMMEDIATE
  → 读取并校验当前数据库事实
  → 检查时间、营业时间和服务能力
  → 检查重叠 busy 排班
  → 同步更新 appointments 与 stylist_schedules
  → 事务提交（COMMIT）

任一步失败
  → 回滚（ROLLBACK）
```

数据库还使用以下一致性保护：

* INSERT 和 UPDATE 触发器阻止同一发型师的重叠 `busy` 排班；
* 相邻时间允许写入；
* 不同发型师同一时间允许写入；
* `expected_version` 与数据库 `version` 不一致时返回 `stale_state`；
* 最终确认重新读取数据库，不信任旧候选。

这些机制保护的是当前单个 SQLite 数据库中的并发写入一致性，不是分布式锁、分布式事务或跨数据库事务。其他会话或并发请求仍可能先完成提交，因此冲突必须在最终事务内复查。

当前结构适合单应用实例、单 SQLite 数据库的工程原型。多实例部署需要服务型数据库、共享会话，并重新评估事务隔离和并发策略。

## 基于 MCP 的 RAG 知识咨询

主项目是 MCP 客户端（Client），独立 [MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service) 是 MCP 服务端（Server）：

```text
ConsultantAgent
  ↓
MCPKnowledgeGateway
  ↓  MCP ClientSession / stdio
MCP Knowledge Service
  ↓
向量检索 + BM25
  ↓
RRF
  ↓
回答 + 引用来源
```

FastAPI 应用生命周期（lifespan）管理 MCP 子进程、`initialize`、`list_tools` 和会话复用；主项目调用真实工具 `query_knowledge_hub`。

MCP 负责标准化跨进程工具调用，RAG 是知识服务内部的检索、融合和引用链路。ChromaDB 与 BM25 索引位于独立知识服务，不属于主项目的预约数据库。

RAG 不参与价格、时长、发型师能力、排班、冲突或预约成功判断。

## 天气后处理

天气服务使用 Open-Meteo，不需要 API Key，默认查询上海。`.env.example` 默认 `WEATHER_ENABLED=true`。

调用顺序为：

```text
聊天预约成功写入 SQLite
→ 获得真实 appointment_id
→ 查询预约时段天气
→ 可选地追加中文提醒
```

天气不属于 MCP 或 RAG。候选搜索、等待确认、预约失败和 REST 预约创建接口不会触发天气；天气失败只省略提醒，不回滚已提交预约。

## 故障边界

| 故障 | 结果 |
| --- | --- |
| LLM 分类不确定 | 规则和活动会话状态继续保护明确预约流程 |
| MCP Knowledge Service 不可用 | 知识咨询返回稳定 503；预约功能不受影响 |
| RAG 无结果 | 返回明确的空结果或降级信息，不编造排班 |
| Open-Meteo 不可用 | 省略天气提醒，已保存预约保持成功 |
| SQLite 写入失败 | 整个事务回滚，不返回预约成功 |
| 候选确认前发生并发写入 | 最终复查返回冲突，不产生重复 `busy` 排班 |

## 身份与生产化边界

业务服务使用 `appointment_id` 与 `owner_id` 联合查询，在正常业务流程中限制跨预约归属的读取和修改。

账户 REST 与聊天请求的预约归属来自 JWT `sub`，客户端身份字段不能覆盖。游客预约归属仍来自浏览器 localStorage，理论上可以被伪造，因此游客路径只提供兼容的业务范围，不是安全认证，也不提供跨设备同步。

当前本地账户是认证 MVP，支持 SQLite 持久化认证会话、刷新令牌哈希轮换和当前会话吊销，但尚无邮箱验证、密码重置、MFA、OAuth、RBAC、设备管理 UI、退出全部设备、Redis 会话或跨设备游客同步。生产化还需要 HTTPS、密钥生命周期管理、分布式限流、PostgreSQL/Redis 或独立身份服务、可观测性和审计能力；当前项目不声称已经具备这些基础设施。

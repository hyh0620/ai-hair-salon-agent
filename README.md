# AI Hair Salon Agent

## 自然语言驱动的理发店预约与知识咨询系统

用户可以通过自然语言查询真实档期、选择发型师，以及创建、查询、取消或修改预约。

系统采用中心路由协调的 Agent 组件理解用户表达并维护多轮对话状态；确定性业务服务负责价格、时长、发型师能力、排班、冲突和预约写入；SQLite 保存预约、排班、账户与认证会话等业务事实。服务知识与护理咨询由独立的 MCP Knowledge Service 提供。

系统同时提供可选本地账户：密码使用 Argon2 哈希，短期访问 JWT 绑定可吊销的服务端认证会话，浏览器通过 HttpOnly Cookie 自动轮换登录凭据，API 客户端可使用 Bearer 令牌。未登录时仍可使用游客预约模式。

> **Agent 负责理解，业务服务负责决策，SQLite 负责保存事实。**

---

## 一分钟项目摘要

真实用户往往不会一次提供完整字段，而是直接表达：

```text
明天下午找擅长冷棕色的老师
今天下午哪些理发师有空？
周五晚上想染发，预算四百左右
就选刚才第二个
```

这类输入包含相对日期、模糊时段、服务偏好、专长要求和多轮上下文。项目将自然语言转换为日期、精确时间或时间范围、服务项目、发型师和专长偏好等结构化约束，再通过真实服务目录、发型师资料和 SQLite 排班生成候选。

LLM 不决定标准价格、服务时长、发型师是否有空或预约是否成功。用户选定候选并确认后，`AppointmentService` 会重新校验业务规则，并在数据库事务中同步写入预约和排班。

知识咨询通过独立 MCP Knowledge Service 完成。MCP 服务不可用时，知识咨询链路返回明确降级结果，预约链路仍可独立运行。

当前主分支基线：

* Python 3.12；
* 403 个自动化测试全部通过，0 个失败，0 个警告；
* 可复现依赖约束与 GitHub Actions；
* `main` 分支必需检查项：`Python 3.12`。

---

## 为什么不是普通预约表单或固定工作流？

普通表单适用于用户已经明确知道服务、时间和发型师的标准预约。现实用户经常使用模糊自然语言，只提供部分信息，并在咨询与预约之间切换。本项目使用 Agent 理解表达、补全槽位和维护上下文，再由确定性服务查询真实档期并执行预约，因此兼顾自然语言体验与业务结果可靠性。

它解决的业务问题包括：

1. 减少前台反复确认时间、服务、预算和发型师偏好的沟通成本；
2. 将知识咨询、档期查询和预约操作统一到一个自然语言入口；
3. 让 LLM 只承担概率性理解，价格、排班、冲突和写入继续由确定性服务控制。

普通表单仍然适合信息完整、流程固定的预约；本项目的价值不在替代表单，而在处理模糊需求、多轮补全、咨询与预约路由以及可信执行。

## 项目阅读导航

* [5 分钟项目演示](docs/DEMO_GUIDE.md)：从业务问题、核心流程和架构边界开始；
* [系统架构与技术边界](docs/ARCHITECTURE.md)：说明事务、身份、会话和 MCP/RAG 等技术细节；
* [测试与 RAG 评估](docs/EVALUATION.md)：区分功能契约、自动化回归和检索质量；
* [本地运行与深度演示手册](docs/DEMO_RUNBOOK.md)：用于安装、数据准备、认证验证和故障复现。

快速了解项目时，建议先查看 5 分钟项目演示；事务、身份与知识检索等技术细节可继续阅读架构和评估文档，本地复现与故障验证参见运行手册。

---

## 业务问题

预约交互需要处理以下情况：

* 用户只提供部分信息，需要多轮补充；
* “下午”“晚上”代表时间范围，而不是固定开始时间；
* “冷棕色”“显白发色”等表达需要映射到结构化服务和专长；
* “第一个”“刚才那个老师”必须结合当前会话理解；
* 未指定发型师时，应返回真实候选，不能自动分配第一位人员；
* 候选查询和最终确认之间可能发生并发冲突；
* 查询、取消和修改预约需要状态、所有权范围和版本校验；
* 登录账户的预约身份必须来自服务器验证的令牌，不能信任客户端声明的预约归属；
* 知识咨询与预约执行使用不同的数据来源和可靠性边界。

项目的核心目标是将模糊需求转换为可验证的业务约束，同时保证价格、排班、冲突和数据库写入由确定性服务控制。

---

## 技术方案

项目使用规则预路由、会话状态和 LLM 辅助理解相结合的方式，而不是把全部路由和业务决策交给模型。

* 明确的预约状态和高精度业务意图由后端预路由优先处理；
* 活动中的预约流程优先于当前单句分类；
* 日期、时间和部分可用性表达使用确定性解析；
* LLM 辅助处理更开放的分类、槽位提取和自然语言回复；
* `SERVICE_CATALOG`、`AvailabilityService` 和 `AppointmentService` 控制业务事实与写入结果。

项目包含 `TaskClassificationAgent`、`AppointmentAgent` 和 `ConsultantAgent` 等按职责拆分的 Agent 组件。它们由同一应用进程中的中心路由和共享会话状态协调，不是独立部署、自由协商或自主规划的分布式 Agent。

---

## 系统架构

![系统架构](./architecture.svg)

### 可选账户身份层

`AuthService` 使用 `pwdlib` 的 Argon2 实现注册与密码验证，并使用 PyJWT 签发固定为 HS256 的短期访问令牌（Access Token）。JWT 包含内部 `sid`，每次认证请求除校验签名、签发方、受众和到期时间外，还会检查 SQLite 中对应的认证会话（Auth Session）是否属于当前用户、未撤销且未过期。默认访问令牌有效期为 15 分钟；旧版本中不含 `sid` 的 JWT 升级后会失效，用户需要重新登录一次。

刷新令牌（Refresh Token）是使用 `secrets` 生成的高熵不透明随机值，只放入路径为 `/api/auth` 的 HttpOnly Cookie；数据库仅保存 SHA-256 哈希，响应 JSON 和 JavaScript 均不可读取原值。`POST /api/auth/refresh` 使用 Cookie 容器与双提交 CSRF，原子消费旧令牌并签发新的访问令牌和刷新令牌，不提供 JSON 请求体刷新令牌流程，也不会延长认证会话的 30 天绝对寿命。

同一个刷新令牌在 3 秒宽限窗口内的并发重复返回 409，不再次轮换；窗口外重放会撤销受影响的整个认证会话。退出登录（Logout）会撤销当前服务端认证会话，因此该会话下已复制的 Bearer 令牌立即失效；同一账户的其他认证会话保持有效。浏览器在普通同源请求收到 401 时采用单请求合并（single-flight）刷新，成功后最多重试原请求一次。

登录、注册和刷新入口使用当前应用进程内的有界滑动窗口限流。登录同时限制单个直连客户端的总请求频率，以及同一客户端与同一规范化邮箱的组合频率；注册和刷新限制单个直连客户端的请求频率。被限制的请求在用户查询、Argon2 或认证会话数据库查询前返回 HTTP 429，并提供 `Retry-After`。

客户端范围只来自 `request.client.host`，不信任 `X-Forwarded-For`、`Forwarded` 或 `X-Real-IP`。部署在反向代理后，需要显式配置可信代理链，或在反向代理/API 网关层执行限流。当前限流器状态仅保存在单个进程内，应用重启后清空，多个工作进程（Worker）或实例之间不共享；生产环境仍需要 Redis、API 网关或反向代理提供分布式限流，不能把当前实现视为完整生产防护。

所有预约入口先构造统一 `RequestIdentity`：有效 JWT 对应的预约归属为 `account:<user_uuid>`，并覆盖客户端提交的 `owner_id` 或 `user_id`；无凭据时则校验普通游客归属。Cookie 认证的状态变更请求还需要双提交 CSRF 令牌。认证未配置时，认证 API 返回 503，但游客预约、知识咨询和其他组件仍可运行。

用户行为分析与回访提醒复用同一个 `RequestIdentity`。登录账户按 `account:<user_uuid>` 隔离，游客按当前浏览器的 `anonymous_owner_id` 隔离；客户端 `user_id` 不能选择分析范围，`X-Anonymous-Owner-ID` 也不能覆盖已验证账户。无效 JWT 返回 401，不会降级为游客。页面只展示账户显示名称或“游客”，不展示内部预约归属、UUID 或令牌。

游客历史不会在登录后自动合并到账户，当前也不提供游客历史认领功能。退出登录后，浏览器仍可使用原匿名归属标识返回游客数据范围。

### Agent 与对话层

负责：

* 业务意图识别；
* 槽位填充（Slot Filling）；
* 相对日期、精确时间和模糊时间范围解析；
* 服务、发型师和专长偏好提取；
* 对话会话状态维护；
* 候选选择和最终确认语义；
* 用户可读回复组织。

不负责决定标准价格、标准时长、真实排班、最终冲突或预约结果，也不直接写数据库。

### 确定性预约业务层

#### `SERVICE_CATALOG`

服务目录是标准价格和时长的权威来源：

| 服务 | 标准时长 | 标准价格 |
| --- | ---: | ---: |
| 男士短发 | 45 分钟 | 88 元 |
| 女士剪发 | 60 分钟 | 128 元 |
| 染发 | 150 分钟 | 398 元 |
| 烫发 | 180 分钟 | 468 元 |

模型输出不会覆盖服务目录。

#### `AvailabilityService`

根据服务支持、发型师专长、营业时间、完整服务时长、当前时间和 SQLite 排班生成稳定候选。精确时间与时间范围使用同一服务查询；该服务不调用 LLM、MCP 或天气，也不写数据库。

#### `AppointmentService`

统一处理预约创建、查询、取消和修改，并负责：

* 所有权范围、状态与版本校验；
* 开始时间、营业时间和发型师服务能力校验；
* 最终冲突检查；
* 预约与排班的原子提交或回滚。

聊天和 REST API 共享相同的确定性业务服务，不直接修改预约生命周期数据。

### SQLite 与并发保护

系统通过以下机制保护单个 SQLite 数据库中的写入一致性：

* SQLite 自增整数 `appointment_id`；
* `BEGIN IMMEDIATE` 写事务；
* 最终写入前的二次冲突检查；
* `appointments` 与 `stylist_schedules` 同事务更新；
* SQLite Trigger 阻止同一发型师出现重叠的 `busy` 排班；
* `version` 乐观并发控制；
* 失败时完整回滚。

这些机制不是分布式锁、分布式事务或跨数据库事务。当前实现适合单应用实例和单 SQLite 数据库的原型验证；多实例生产部署需要迁移到 PostgreSQL 等服务型数据库，并重新设计共享会话和并发策略。

### 基于 MCP 的 RAG 知识咨询

主项目通过 `MCPKnowledgeGateway` 和 MCP `ClientSession`，使用 stdio 调用独立 MCP Knowledge Service。知识服务内部执行：

* 向量检索（Dense Retrieval）；
* BM25 关键词检索；
* RRF（Reciprocal Rank Fusion）结果融合；
* 引用来源（Citations）。

MCP 解决主项目如何标准化调用独立知识服务；RAG 解决知识服务内部如何检索、融合并返回相关知识。ChromaDB 位于独立 MCP Knowledge Service 中，不是主项目直接维护的预约数据库。

该链路用于服务知识、门店政策、护理和发型建议，不参与真实排班、价格、时长、冲突或预约结果判断。

### 天气后处理

天气使用无需 API Key 的 Open-Meteo，默认地点为上海。`.env.example` 中 `WEATHER_ENABLED=true`，天气仅在聊天预约已经成功写入并获得真实 `appointment_id` 后调用。

天气是非阻塞的上下文增强，不属于 MCP 或 RAG，也不参与预约事实判断。调用失败时只省略提醒，不撤销预约；REST 预约创建接口不会自动追加天气。

---

## 一次预约如何执行

以输入“明天下午找擅长冷棕色的老师”为例：

1. 识别为 `search_availability`；
2. 将“明天”解析为具体日期；
3. 将“下午”保留为时间范围；
4. 将“冷棕色”规范化为染发服务下的专长偏好；
5. 从 `SERVICE_CATALOG` 读取标准时长和价格；
6. `AvailabilityService` 查询支持服务、专长匹配且无冲突的真实候选；
7. 用户通过序号、姓名或“姓名 + 时间”选择候选；
8. 系统展示预约摘要并请求最终确认；
9. `AppointmentService` 重新检查时间、服务能力和冲突；
10. 同一事务内写入预约与排班，返回真实 `appointment_id`；
11. 聊天链路可在成功后追加上海天气提醒。

系统不会自动选择第一位可用发型师，也不会在最终确认前写入数据库。

---

## 多轮会话与预约身份

```text
用户：预约明天
系统：记录日期，继续询问服务和时间

用户：男士短发
系统：从服务目录获得 45 分钟和 88 元，继续询问时间

用户：下午两点
系统：查询明天 14:00 的真实候选

用户：第一个
系统：结合当前会话解析候选选择并请求最终确认

用户：确认
系统：再次检查冲突，在事务内写入预约和排班
```

浏览器分别维护两个标识：

* 认证会话是服务端持久化登录状态，由访问 JWT 的内部 `sid` 引用，不展示给用户，也不参与预约归属；
* `chat_session_id` 对应 `salon_chat_session_id`，只隔离进程内对话状态、活动预约状态和候选列表；
* `anonymous_owner_id` 对应 `salon_anonymous_owner_id`，用于限定预约的查询、修改和取消范围；
* 清空对话只重置 `chat_session_id`，不会更换 `anonymous_owner_id`，因此已保存预约仍可访问；
* 旧页面只有会话 ID 时，首次升级会将该值复制为初始匿名归属标识，以兼容此前创建的预约。

登录后，预约归属不再来自浏览器存储，而是由后端根据 JWT `sub` 生成 `account:<user_uuid>`。`sid` 仅定位可吊销的认证会话，不是 `chat_session_id`，也不会改变 `RequestIdentity.owner_id`。注册、登录和退出都会轮换 `chat_session_id`，刷新不会；`anonymous_owner_id` 不会被删除，退出后仍可返回原游客空间。

当前对话会话：

* 隔离进程内的对话状态、活动预约状态和候选列表；
* 同一会话的请求通过异步锁串行处理；
* 注册表具有 TTL 和容量限制；
* 支持浏览器显式重置。

> 认证会话管理登录凭据的有效性，`chat_session_id` 管理短期对话状态，`anonymous_owner_id` 管理可被客户端伪造的游客业务范围；三者互不替代。

对话会话本身不是登录凭证，也不是持久化用户记忆或 Redis 分布式会话。应用重启后进程内对话状态不会保留；认证会话和刷新令牌哈希则持久化在 SQLite。浏览器保存的匿名归属标识仍可用于管理 SQLite 中已有游客预约，且不会在登录后自动转移到账户。

---

## 预约生命周期

预约创建、查询、取消和修改共享 `AppointmentService`：

```text
查询当前预约归属范围内的预约
    ↓
选择预约并读取当前 version
    ↓
提交 expected_version 和修改内容
    ↓
最终确认
    ↓
BEGIN IMMEDIATE
    ├── 预约归属范围检查
    ├── 状态检查
    ├── version 检查
    ├── 服务能力检查
    ├── 时间与营业时间检查
    ├── 冲突检查
    ├── 更新 appointments
    └── 更新 stylist_schedules
    ↓
COMMIT 或完整 ROLLBACK
```

取消预约时不删除历史记录，而是将预约和对应排班更新为 `cancelled`。修改预约时，未提供字段保持不变；服务变化后重新计算标准价格、时长和结束时间；成功后递增 `version`。

SQLite、业务服务和 REST API 使用稳定的英文状态码；客户聊天与页面使用中文状态标签。`version` 继续用于乐观并发控制，但不在普通客户聊天中展示。

聊天中的“取消本次操作”只清理当前会话的未完成槽位和候选，不读取或修改数据库；“取消预约”则进入已保存预约的生命周期流程，并在用户最终确认后执行原子取消。单独输入“取消”不会直接取消数据库预约。

如果请求携带的 `expected_version` 已过期，服务返回 `stale_state`，避免后提交的请求静默覆盖较新修改。

---

## 关键工程设计

### 概率性理解，确定性执行

| 业务事实 | 权威来源 |
| --- | --- |
| 服务、价格和时长 | `SERVICE_CATALOG` |
| 发型师资料与专长 | 结构化发型师数据 |
| 已有预约与排班 | SQLite |
| 可用候选 | `AvailabilityService` |
| 冲突检查与预约写入 | `AppointmentService` |
| 预约归属、状态与版本范围 | `AppointmentService` + SQLite |
| 护理知识和服务政策 | MCP Knowledge Service |

LLM 的作用是理解和组织，不是替代业务事实来源。

### 最终确认与并发复查

候选生成后，其他会话或并发请求可能抢先完成同一档期的预约。因此最终确认会重新检查发型师、服务能力、营业时间和冲突，再由数据库触发器（Trigger）做最后保护。

### 故障隔离

* MCP Knowledge Service 不可用时，知识咨询返回明确的不可用响应；
* 预约链路不依赖 MCP，仍可执行；
* 天气失败只省略提醒，不改变已保存预约；
* 数据库写入失败时不会返回预约成功；
* RAG 结果不会覆盖服务目录或排班事实。

---

## 当前能力

| 能力 | 当前实现 |
| --- | --- |
| 自然语言意图 | 识别 `create_booking`、`search_availability`、`consultation` 及预约生命周期操作 |
| 日期和时间 | 支持相对日期、具体日期、精确时间和模糊时段 |
| 多轮槽位 | 已知槽位保存在当前会话，活动流程优先于单句分类 |
| 服务目录 | 使用确定性目录管理服务、价格和标准时长 |
| 专长映射 | 将已支持的自然语言别名规范化为结构化专长 |
| 真实档期 | 根据服务能力、营业时间、时长和 SQLite 排班生成候选 |
| 候选与确认 | 支持序号、姓名和时间表达；歧义时继续追问；确认后才写入 |
| 预约生命周期 | 支持查询、取消、修改服务、更换发型师和改期 |
| 并发一致性 | 使用事务、二次检查、触发器和 `version` |
| 可选账户身份 | Argon2、短期 JWT `sid`、服务端认证会话、刷新令牌轮换、HttpOnly Cookie 与 CSRF |
| 知识咨询 | 通过 MCP 调用独立 RAG 知识服务并返回引用来源 |
| 天气提醒 | 聊天预约成功后查询 Open-Meteo 上海预报，失败不影响预约 |

---

## 测试与评估

### 自动化测试

| 项目 | 当前结果 |
| --- | ---: |
| pytest | 403 passed |
| 失败 | 0 |
| 警告 | 0 |

```bash
python -m pip check
bash scripts/test_hermetic.sh
python -m compileall agents api services db config eval web tests
```

普通 pytest 是隔离式（Hermetic）测试：`tests/conftest.py` 在应用模块导入前设置
`EXTERNAL_CALL_POLICY=deny`，使用临时 SQLite、模拟 LLM、模拟天气服务、
模拟 MCP 服务和固定时间，并阻断非本机网络与外部 DNS。CI 不读取外部服务密钥，
也不依赖用户本地数据库或真实外部服务。

### 已保存的评估快照

| 指标 | 结果 |
| --- | ---: |
| 功能契约 | 28 / 28 |
| RAG 用例 | 11 |
| Hit@1 | 10 / 11 |
| Hit@3 | 11 / 11 |
| MRR | 0.9545 |
| 引用来源与预期来源匹配 | 11 / 11 |

评估语料包括 7 份源文档和 24 个语义切片。该快照用于受控语料下的可复现功能与检索回归，不代表生产准确率，也不是通用基准测试。详见[评估方法与结果](docs/EVALUATION.md)。

---

## 技术栈

| 层次 | 技术 |
| --- | --- |
| Web 与 API | Python 3.12、FastAPI、Uvicorn、Jinja2、Pydantic |
| 账户身份 | pwdlib / Argon2、PyJWT、HttpOnly Cookie、Bearer、CSRF |
| Agent 与模型 | LangChain Core、LangChain OpenAI、OpenAI-compatible LLM / Qwen |
| 业务与数据 | SQLAlchemy、SQLite、事务、触发器、乐观并发控制 |
| 知识咨询 | 官方 MCP Python SDK、stdio、ChromaDB、向量检索、BM25、RRF、引用来源 |
| 外部上下文 | Open-Meteo |
| 工程质量 | pytest、GitHub Actions、pip 约束文件、必需检查项 |

---

## 项目结构

```text
ai-hair-salon-agent/
├── agents/                  # 任务分类、预约、咨询和多轮状态
├── api/                     # FastAPI 业务接口与响应模型
├── services/                # 服务目录、档期、预约和 MCP Gateway
├── db/                      # SQLAlchemy 模型、Repository 和 SQLite
├── config/                  # 模型、时间、日志和应用配置
├── web/                     # 聊天、状态与排班页面
├── tests/                   # 单元、集成和回归测试
├── eval/                    # 基准用例集与评估工具
├── docs/                    # 架构、演示、评估与集成文档
├── architecture.svg
├── requirements.txt
├── requirements-dev.txt
├── constraints-py312.txt
└── app.py
```

---

## 快速启动

主要开发、运行和 CI 版本为 Python 3.12。完整环境安装、私有外部服务配置和知识服务数据准备见[本地运行与深度演示手册](docs/DEMO_RUNBOOK.md)。公开示例配置不包含真实 LLM 或 Embedding 凭据，RAG MCP 默认关闭，因此预约功能与本地页面可以先独立启动。

完成环境准备后启动 FastAPI：

```bash
python -m uvicorn app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --no-proxy-headers
```

账户功能需要在本地 `.env` 中为 `AUTH_JWT_SECRET` 设置至少 32 个随机字节，并保持该文件不进入 Git。未配置有效密钥时，账户 API 返回 503，游客预约仍可使用；HTTPS 部署还应设置 `AUTH_COOKIE_SECURE=true`。浏览器凭据刷新使用 HttpOnly Cookie 和 CSRF；Swagger/CLI 登录返回的 Bearer 令牌仍可直接使用，Cookie 刷新需要支持 Cookie 容器。

常用入口：

* 首页：`http://127.0.0.1:8000`
* Swagger：`http://127.0.0.1:8000/docs`
* 发型师信息：`http://127.0.0.1:8000/stylists`
* 发型师排班：`http://127.0.0.1:8000/stylist-schedule`
* 系统状态：`http://127.0.0.1:8000/status`
* 健康检查：`http://127.0.0.1:8000/health`

---

## 开发与测试

在运行手册准备好的开发环境中执行：

```bash
python -m pip check
bash scripts/test_hermetic.sh
python -m compileall agents api services db config eval web tests
```

依赖文件职责：

* `requirements.txt`：直接运行依赖及兼容范围；
* `requirements-dev.txt`：开发、测试和评估依赖；
* `constraints-py312.txt`：Python 3.12 下已验证的完整精确版本。

`constraints-py312.txt` 本身不会安装软件，需要与对应的 `-r` 文件一起使用。

### 外部服务隔离

普通测试和 CI 统一使用：

```bash
bash scripts/test_hermetic.sh
```

不连接外部服务的本地 UI 验收使用临时 SQLite 和测试认证配置：

```bash
bash scripts/run_isolated_validation.sh
```

该脚本仅绑定 `127.0.0.1`，退出时清理临时数据库。真实 LLM、Embedding、
MCP 或天气集成验证必须在独立的显式流程中设置 `EXTERNAL_CALL_POLICY=allow`；
它不属于默认 pytest 或必需检查项。不要在测试命令、日志或仓库文件中写入真实密钥。

---

## 项目边界与后续规划

### 身份与所有权边界

业务服务使用 `appointment_id` 与 `owner_id` 联合查询，在正常业务流程中限制跨预约归属的读取和修改。

登录账户的预约归属由后端从已验证 JWT 的 `sub` 生成，客户端提交的 `owner_id` 或 `user_id` 不会覆盖登录身份。账户之间的查询、取消和修改按 `account:<user_uuid>` 隔离；对外将不存在和不属于当前账户统一返回 `not_found`。

游客模式继续使用浏览器 localStorage 中的 `anonymous_owner_id`。该值可以被读取或伪造，因此只构成兼容的游客业务范围，不是认证，也不提供跨设备同步。游客不能使用 `account:` 命名空间，游客预约也不会自动归入后续注册的账户。

### 当前生产化边界

当前尚未实现：

* 邮箱验证、密码重置和登录设备管理 UI；
* MFA、OAuth 与 RBAC；
* Redis 分布式会话；
* PostgreSQL 服务型数据库；
* 支付系统；
* 门店员工端和完整排班管理后台；
* 生产级监控、告警和审计；
* 真实商业流量验证。

当前账户能力是可信预约归属的认证 MVP，不是完整生产身份系统。服务端认证会话支持当前会话吊销、一次性刷新令牌轮换和重放响应，但当前进程内限流不跨工作进程或实例共享，SQLite 也不是多实例身份存储。生产部署仍需要 HTTPS、密钥生命周期管理、PostgreSQL/Redis 或独立身份服务、分布式限流、审计和更完整的账号安全策略。项目不声称已经完成生产部署。

---

## 详细文档

* [系统架构](docs/ARCHITECTURE.md)
* [演示指南](docs/DEMO_GUIDE.md)
* [本地运行与深度演示手册](docs/DEMO_RUNBOOK.md)
* [评估方法与结果](docs/EVALUATION.md)
* [MCP 知识服务集成](docs/RAG_SERVICE_INTEGRATION.md)
* [独立 MCP Knowledge Service](https://github.com/hyh0620/mcp-knowledge-service)

## v1.0 发布与验收

v1.0 发布材料和验收记录包括自动化发布门禁、真实外部服务端到端验收、演示流程与正式版本说明。正式版本和发布时间以 GitHub Releases 页面为准。

隔离式（Hermetic）CI 禁止真实外部调用，用于验证可复现的业务、事务、认证和故障边界；真实外部服务验收在显式允许外部调用的隔离流程中执行，不属于隔离式 CI。已完成的真实外部服务验收确认：

* OpenAI-compatible Qwen `qwen-plus` 真实调用成功；
* MCP stdio 初始化和 `query_knowledge_hub` 调用成功；
* 知识咨询返回 4 个引用来源，并命中预期知识源；
* Open-Meteo 在预约事务提交并生成真实 `appointment_id` 后调用成功；
* MCP 故障时知识咨询明确降级，预约功能保持可用；
* Chroma 查询前后知识集合 ID、24 个语义切片、7 份文档、24 个向量、元数据和 BM25 逻辑内容不变；
* Chroma SQLite/HNSW 文件变化属于预期运行时持久化行为，不代表业务数据修改。

相关材料：

* [发布流程与验证清单](docs/RELEASE_CHECKLIST.md)
* [v1.0 版本说明](docs/RELEASE_NOTES_V1.0.md)
* [演示指南](docs/DEMO_GUIDE.md)
* [GitHub Releases](https://github.com/hyh0620/ai-hair-salon-agent/releases)

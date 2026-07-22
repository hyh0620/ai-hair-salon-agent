# v1.0 发布流程与验证清单

本文件是可重复使用的版本发布流程模板。每次发布应在独立的实际执行记录中勾选结果；仓库中的空复选框不代表当前版本验收失败。当前 v1.0 验收摘要见 [`../README.md`](../README.md) 和 [`RELEASE_NOTES_V1.0.md`](RELEASE_NOTES_V1.0.md)，正式版本状态以 [GitHub Releases](https://github.com/hyh0620/ai-hair-salon-agent/releases) 为准。

只有全部门禁通过，并完成明确隔离的真实 Provider 验收后，才可以创建 Tag 和 GitHub Release。

## A. Git 与版本基线

- [ ] `main` 与 `origin/main` 指向同一 Commit。
- [ ] 工作区干净。
- [ ] Required Check 为 `Python 3.12`，且结果成功。
- [ ] 所有计划纳入 v1.0 的 PR 已合并。
- [ ] 没有待处理的发布阻断问题。
- [ ] 最终 Tag 指向已经通过全部发布门禁的 `main` Commit。
- [ ] 创建 Tag 前重新记录并复核完整 Commit SHA。

## B. Hermetic 自动化验证

在已激活的 Python 3.12 开发环境中执行：

```bash
python -m pip check
bash scripts/test_hermetic.sh
python -m compileall agents api services db config eval web tests
git diff --check
```

- [ ] 全部测试通过。
- [ ] 0 failures。
- [ ] 0 warnings。
- [ ] 没有真实 Provider 调用。
- [ ] 没有应用外部网络访问。
- [ ] 没有真实数据库访问。
- [ ] 没有读取真实 `.env`。

## C. 隔离运行验证

使用临时 SQLite、测试认证配置和拒绝外部调用的入口：

```bash
bash scripts/run_isolated_validation.sh
```

- [ ] `GET /health` 返回 200。
- [ ] 首页返回 200。
- [ ] `/status` 返回 200。
- [ ] `/docs` 可以打开。
- [ ] 注册和登录可用。
- [ ] `/api/auth/me` 可用。
- [ ] 用户行为分析可用。
- [ ] 游客模式可用。
- [ ] 进程退出后临时 SQLite 和临时目录被清理。
- [ ] 没有真实 Provider 调用。

## D. 预约业务验收

- [ ] 模糊时间范围查询返回真实候选。
- [ ] 精确时间查询返回真实候选。
- [ ] 服务和专长别名映射到结构化业务条件。
- [ ] 未指定发型师时不自动分配，而是返回候选。
- [ ] 用户选择候选后才进入最终确认。
- [ ] 最终确认后才写入数据库。
- [ ] `appointment_id` 是真实数据库 ID。
- [ ] 同一发型师的重叠时段被拒绝。
- [ ] 不同发型师同一时间可以预约。
- [ ] 同一发型师相邻时段可以预约。
- [ ] 创建、查询、修改和取消流程可用。
- [ ] `version` 乐观并发冲突返回稳定结果。
- [ ] 预约与排班在同一事务中更新。
- [ ] 任一步失败时完整回滚。
- [ ] 账户 A 与账户 B 的预约所有权隔离。
- [ ] 游客空间与账户空间隔离。

## E. 认证验收

- [ ] 注册可用。
- [ ] 登录可用。
- [ ] 登录和注册限流生效。
- [ ] Access JWT 包含内部 `sid`。
- [ ] Access Token 使用短期默认有效期。
- [ ] Refresh Cookie 为 HttpOnly。
- [ ] Refresh Token 不进入 JSON 响应。
- [ ] 数据库只保存 Refresh Token Hash。
- [ ] Refresh Token 在单个写事务中轮换。
- [ ] Grace Window 内并发重复刷新返回冲突且不误撤销 Session。
- [ ] Grace Window 外重放撤销当前 Auth Session。
- [ ] Logout 立即使该 Session 中复制的 Bearer Token 失效。
- [ ] Access 过期后仍可通过 Refresh Cookie 完成 Logout。
- [ ] 同一账户的多个 Auth Session 相互独立。
- [ ] Cookie 写请求和 Refresh 执行 CSRF 校验。
- [ ] 代理转发头不能绕过认证限流。
- [ ] Chat Session 与 Auth Session 相互独立。
- [ ] Logout 后保留 `anonymous_owner_id`。

## F. 真实 Provider 验收

每次需要验证真实集成时，都应使用隔离环境和私有配置，并在完成后恢复安全配置。真实 Provider 验收不属于 Hermetic CI，不能用 pytest Mock 结果替代。

- [ ] 显式设置 `EXTERNAL_CALL_POLICY=allow`。
- [ ] Qwen 分类或回复成功。
- [ ] MCP Knowledge Service 成功初始化。
- [ ] `query_knowledge_hub` 可以调用。
- [ ] 冷棕色咨询返回 Citations。
- [ ] RAG 来源符合当前受控语料。
- [ ] MCP 故障时 Booking 仍可运行。
- [ ] Open-Meteo 只在聊天预约成功后调用。
- [ ] 天气失败不撤销已经保存的预约。
- [ ] 验收结束后恢复拒绝外部调用的安全配置。
- [ ] 截图和日志不包含任何 Provider Key。

## G. 演示验收

- [ ] 3 至 5 分钟演示可以完整跑通。
- [ ] 演示数据库使用可控数据。
- [ ] 现场演示不依赖临时修改代码。
- [ ] 已准备 Booking 演示。
- [ ] 已准备 Consultation 与 Citations 演示。
- [ ] 默认演示聚焦业务价值、Agent 边界和可信预约执行。
- [ ] 认证 Session 与故障注入作为备用深度演示准备完毕。
- [ ] 已准备项目限制说明。
- [ ] 不展示 Secret。
- [ ] 不展示真实用户数据。

## H. 发布操作

只有 A 至 G 全部通过后，才允许使用已经记录的验证 Commit 创建并推送 Tag：

```bash
git tag -a v1.0.0 <VERIFIED_COMMIT> -m "AI Hair Salon Agent v1.0.0"
git push origin v1.0.0
```

Tag 推送后再根据 [`RELEASE_NOTES_V1.0.md`](RELEASE_NOTES_V1.0.md) 创建 GitHub Release。

## I. 发布后检查

- [ ] Tag 指向正确的已验证 Commit。
- [ ] GitHub Release 可见。
- [ ] README 链接正常。
- [ ] Release Notes 排版正常。
- [ ] 安装和启动命令可以直接复制执行。
- [ ] Release 中没有敏感文件或本地运行数据。
- [ ] `main` 工作区保持干净。
- [ ] 发布后的 CI 仍然成功。

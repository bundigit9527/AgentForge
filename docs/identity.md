# 本地身份与权限

当前实现本地租户、空间、用户、空间成员角色、访问令牌和显式共享，保留企业登录适配接口。已增加原版 Rakazo 邮箱密码登录与浏览器令牌桥接（见 [原版 Web 接入](rakazo-web.md)），没有 OIDC / SAML、MFA 或完整权限审计；初始化与签发令牌由拥有数据库配置的运维人员执行。API 不提供匿名注册或任意用户令牌签发接口。

## 配置与初始化

`AGENTFORGE_AUTH_MODE` 支持两个值，API 与 Worker 应配置一致：

| 模式 | 身份来源 | 用途 |
| --- | --- | --- |
| `development`（默认） | `AGENTFORGE_DEV_TOKEN` 对应固定 `DEV_TENANT / DEV_SPACE / DEV_USER` | 单用户本地调试；角色固定为 member，不开放数据库成员和令牌管理接口 |
| `database` | 数据库令牌及实时成员关系 | 本地多用户联调；不接受开发令牌，不从请求正文或自定义身份头读取权限 |

开发模式不是生产认证。多用户环境启用 database 模式；两个模式不要面向同一数据库同时提供对外访问。令牌通过 `Authorization: Bearer ...` 传递，对外访问需使用 HTTPS。

以下步骤在仓库根目录手动执行。它们会修改指定数据库，本次代码更新没有对开发库执行这些命令。

1. 在已有 `.env` 中设置 `AGENTFORGE_AUTH_MODE=database`，保留数据库配置。
2. 显式升级数据库，不通过应用启动自动建表：

```bash
uv run --env-file .env --locked alembic upgrade head
```

3. 初始化一个空间及其首位管理员：

```bash
uv run --env-file .env --locked agentforge-admin bootstrap \
  --tenant local --tenant-name "本地企业" \
  --space development --space-name "开发空间" \
  --user developer --display-name "开发管理员"
```

命令拒绝覆盖已存在的空间。上例沿用默认开发身份的 ID，因此该用户仍拥有原先资源；自定义过开发身份时应使用原 ID。迁移不会猜测资源所有者，也不会自动把旧用户提升为管理员。

4. 为该空间内的活跃成员签发令牌：

```bash
uv run --env-file .env --locked agentforge-admin issue-token \
  --tenant local --space development --user developer \
  --label "本地管理员联调" --hours 24
```

输出含 `token_id`、`token`、`expires_at`（Unix 秒）。原始令牌只在签发时返回，数据库只保存 SHA-256 摘要；列表不会返回摘要或原值。有效期默认 24 小时，可设置 1 分钟至 365 天。不把输出写入仓库或日志；丢失后重新签发并撤销旧令牌。

5. 按需启动本地 API，在另一个终端单次执行 Worker：

```bash
uv run --env-file .env --locked uvicorn agentforge_api.app:create_app --factory --host 127.0.0.1 --port 8000
uv run --env-file .env --locked agentforge-worker --once
```

API 启动为前台进程；两个命令分别在终端执行。数据库与模型配置沿用 [开发入门](development.md)，默认 mock 无需模型密钥。

## 原版 Web 登录

升级至 `0009` 后，为已存在的活跃成员配置邮箱和密码：

```bash
uv run --env-file .env --locked agentforge-admin set-password \
  --tenant example-tenant --space example-space --user alice --email alice@example.com
```

密码由终端隐式输入两次，不作为命令行参数、默认配置或日志输出；至少 12 个字符，UTF-8 编码最多 1024 字节。数据库保存随机盐的 scrypt 摘要（N=131072、r=8、p=1），不会保存明文密码。

原版登录只提供邮箱，因此部署内邮箱大小写归一后必须唯一；每个租户用户映射一个默认登录空间，不从登录正文接收租户或角色。配置命令可调整默认空间，但不能创建成员。重新执行该命令会撤销该用户已有的密码登录会话，保留原有 API 令牌。

连续 5 次错误密码会锁定该登录 15 分钟；同时限制每进程同一连接来源每分钟 30 次尝试和 2 个并发密码计算。反向代理 / 多进程部署应配置统一入口限流，进程内限制不等于集群级限流。未知账号与错误密码返回相同错误。浏览器会话最长 8 小时，受成员停用、密码重设、令牌撤销及过期约束；未勾选 rememberMe 时使用会话 Cookie。

目前不开放匿名注册，原版注册表单收到 `REGISTRATION_CLOSED`；在线改密、密码找回、账号删除和企业 SSO 尚未实现，管理人员使用上述命令重设密码。原版页面的控件和错误处理保持原样。

## 角色与资源边界

| 操作 | admin | member | viewer |
| --- | --- | --- | --- |
| 读取自己或已获空间共享的资源 | 允许 | 允许 | 允许 |
| 创建 Agent、会话、Run；安装 Skill | 允许 | 允许 | 禁止 |
| 使用共享 Agent 创建自己的会话 | 允许 | 允许 | 禁止 |
| 修改、共享自己的资源；取消自己的任务 | 允许 | 允许 | 禁止 |
| 修改、取消或卸载他人的资源 | 禁止 | 禁止 | 禁止 |
| 维护当前空间成员与角色 | 允许 | 禁止 | 禁止 |
| 列出和撤销自己的令牌 | 允许 | 允许 | 允许 |
| 按 token_id 撤销当前空间他人的令牌 | 允许 | 禁止 | 禁止 |

管理员不会自动获得他人私有 Agent 或会话。当前空间的最后一位活跃管理员不能被降级或停用；并发修改也遵守该限制。租户、空间、用户或成员关系失活均会拒绝令牌访问。空间管理员可新增成员或变更其角色，但不会改名其他空间共用的既有租户用户。

资源默认 `private`。所有者可以设为 `space`，对同租户同空间的所有活跃成员开放以下权限：

| 资源 kind | 共享范围 |
| --- | --- |
| `agent` | 读取版本配置；可写成员可创建自己的会话。原所有者的会话和任务不随 Agent 共享 |
| `skill` | 读取已安装副本、绑定到自己的 Agent；不能修改或卸载所有者的副本 |
| `thread` | 读取该会话的消息、现有及后续 Run、事件、尝试和工具记录；不能继续提交或取消 |
| `run` | 读取该 Run 的结果、配置、事件、尝试、工具参数与结果；不授予整个会话的读取权限 |

共享的内容包括被共享资源接口可返回的配置、技能正文或工具业务文本。所有者需按实际资料范围决定共享。Agent 引用私有 Hub Skill 时，其他成员只有同时获该 Skill 授权才可读取或使用 Agent。共享会话、Run 则是所有者对其中已有运行内容的明确公开，不会自动共享原 Agent 或安装目录。

当前仅有空间范围的读取/使用共享，不含指定个人授权、编辑协作或跨空间分享。已授权读取或复制的内容不能通过撤销共享远程收回；自己的历史和固定快照继续保留。

## 接口

所有接口均需 Bearer 令牌；`/docs` 提供请求结构。资源不属于当前授权范围时返回 404，角色不足返回 403，无效、过期或已撤销的令牌返回 401。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/me` | 服务端确认的租户、空间、用户、角色和认证模式 |
| `GET /api/v1/members?offset=0&limit=100` | 管理员分页读取当前空间成员 |
| `PUT /api/v1/members/{user_id}` | 管理员新增或更新成员：`display_name`、`role`、`active`（默认 true） |
| `GET /api/v1/auth/tokens` | 当前身份的令牌元数据，含过期和撤销时间 |
| `DELETE /api/v1/auth/tokens/{token_id}` | 撤销令牌；自身或同空间管理员可操作 |
| `GET /api/v1/sharing/{kind}/{resource_id}` | 所有者查看显式共享状态 |
| `PUT /api/v1/sharing/{kind}/{resource_id}` | 所有者设置 `{"visibility":"space"}` 或 `{"visibility":"private"}` |

`kind` 为 agent、skill、thread、run；`resource_id` 为 UUID。skill 使用安装后返回的 `reference`，内置 Skill 不提供此共享接口。GET sharing 返回直接共享状态；Run 还可能继承所属会话的共享，即使自身显示 private。

Agent、会话和 Skill 列表包含有权读取的资源。Agent 列表在过滤不可读的技能依赖后分页。为共享 Agent 建立的会话由调用方拥有，并继续固定 Agent 版本。

## 双用户手动调试

启动 API 并取得管理员令牌后，把它设置为终端变量 `ADMIN_TOKEN`，地址设为 `BASE=http://127.0.0.1:8000`。以下命令中的令牌变量均由你在本地填入，不提交 Git。

先检查身份，再建立普通成员和只读成员：

```bash
curl -s "$BASE/api/v1/me" -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s -X PUT "$BASE/api/v1/members/alice" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"display_name":"Alice","role":"member"}'
curl -s -X PUT "$BASE/api/v1/members/bob" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"display_name":"Bob","role":"viewer"}'
```

使用 `agentforge-admin issue-token` 分别为 alice、bob 签发令牌，设置终端变量 `ALICE_TOKEN`、`BOB_TOKEN`。创建 Alice 的 Agent：

```bash
curl -s -X POST "$BASE/api/v1/agents" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"资料助手"}'
```

把返回的 `agent_id` 设为 `AGENT_ID`。用 Bob 读取应得到 404；Alice 共享后，Bob 可读取，但创建会话仍返回 403：

```bash
curl -i "$BASE/api/v1/agents/$AGENT_ID" -H "Authorization: Bearer $BOB_TOKEN"
curl -s -X PUT "$BASE/api/v1/sharing/agent/$AGENT_ID" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"visibility":"space"}'
curl -i "$BASE/api/v1/agents/$AGENT_ID" -H "Authorization: Bearer $BOB_TOKEN"
```

管理员将 bob 的 role 改为 member 后，Bob 可按 [Agent 与会话](conversations.md) 创建自己的会话并提交任务；单次 Worker 执行后得到结果。Alice 不能读取 Bob 的私有会话。Bob 也不能更新 Alice 的 Agent、取消 Alice 的任务或调整其共享状态。

再提交一个 Bob 的任务，在 Worker 执行前将 Agent 改回 private。Worker 应将该任务标记为 `failed / access_revoked`，不执行工具。Bob 可读取自己的失败记录；重新共享只允许后续新任务，不自动重跑失败任务。最后通过 DELETE auth/tokens 撤销 Bob 的令牌，再请求 `/me` 应返回 401。

## 执行撤权与恢复

- Run 快照保存提交时的认证模式，不保存可永久复用的角色。Worker 使用自身配置与快照中更严格的模式，数据库模式的任务不能因为 Worker 默认配置而跳过成员检查。
- Worker 在模型检查点、工具开始、结果写回和续租时复查活跃成员的可写角色，以及所引用 Agent、Hub Skill 的可见性。viewer 降级、成员停用或借用资源取消共享，均阻止后续执行。
- 只有当前有效租约能把撤权任务结束为 `access_revoked`；取消、租约失效或重新领取后，旧执行不能覆盖状态。未完成的工具记录标记为 abandoned，迟到结果不写入。
- 检查以数据库当次可见的已提交权限为准，不强制终止正在运行的同步 Python 函数或网络请求。恢复逻辑仍只适用于现有无外部副作用工具；未来业务写操作需要独立审批、幂等和结果核对。
- 令牌过期或撤销停止接口访问与事件推送，不取消此前接受的任务。需要停止任务时取消 Run，或由管理员停用成员 / 降为 viewer。停用不会自动销毁令牌；恢复成员后，未过期、未撤销的令牌可重新使用。
- Skill 卸载仅隐藏未来目录选择；已有固定副本仍受当前授权检查。撤销共享阻止其他成员继续借用，不删除其已保存的历史内容。
- SSE 每轮查询和每条事件发送前复查凭据及资源权限，失效后发送无游标的 `stream_error / access_unavailable` 并关闭。已发送的事件不会被撤回。

## 迁移与企业登录适配

迁移 `0006` 新增 identity_tenants、identity_spaces、identity_users、identity_memberships、identity_tokens、resource_shares，不改写已有 Agent、Skill、会话、Run 或工具记录。旧快照缺省按 development 解释，但 database Worker 仍使用数据库授权。切换前先登记所需的旧身份；不登记就不会获得新模式下的执行权限。

`IdentityProvider.authenticate(token) -> Scope` 是身份适配接口。后续由企业适配器验证外部凭据并映射内部 ID，返回后仍必须经过本地成员检查。当前提供开发令牌、本地数据库令牌、由数据库令牌交换的浏览器会话和测试适配器，没有实现企业登录协议。浏览器会话最多 8 小时，逐次复查原令牌和成员权限；退出仅撤销浏览器会话，不撤销原访问令牌。

## 本轮验证

SQLite 266 项通过、5 项 PostgreSQL 专用用例跳过；PostgreSQL 17.11 独立测试库 271 项通过。覆盖私有资源对管理员仍私有、共享与所有者写入分离、跨租户/空间隔离、只读角色、令牌过期撤销、SSE 批内撤权、运行中撤权丢弃工具结果、并发保留最后管理员、历史数据迁移及命令行初始化。测试均使用模拟模型；真实企业登录与生产运行尚未验收。

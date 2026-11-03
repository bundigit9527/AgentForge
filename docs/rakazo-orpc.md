# 原版 Rakazo 的 Python oRPC 兼容

核查基线：Rakazo `d165de43`。Rakazo 源码保持原样；所有协议、登录和业务兼容在 AgentForge 实现。`/rpc` 和 REST `/api/v1` 直接共用存储、授权、事务、队列、Worker / Harness，不增加 HTTP 自调用或第二套执行循环。安装、登录与浏览器验证见 [原版 Web 接入](rakazo-web.md)。

## 支持范围

| 调用 | 行为 |
| --- | --- |
| `health`、`me`、`bootstrap`、`spaces.list` | 当前授权身份、空间、导航及会话快照 |
| `agentSkills.list/get` | 按权限查看内置、安装和个人技能目录，读取 SKILL.md |
| `agentSkills.create/update/remove` | 原版个人技能维护；固定资产 ID、不可变版本、删除保留历史 |
| `bots.list/get/create/update` | Agent 映射为 Bot；所有者更新名称、指令与展示配置；spawnKey 创建去重 |
| `threads.head/get/messages` | 当前用户的消息、游标、最新运行和向前分页 |
| `threads.send` | 提交时采用最新 Agent 版本，支持原版 `/技能名` 选择并固定 Run 技能快照；clientNonce 去重 |
| `threads.stop` | 取消活动任务 |
| `threads.clear` | 原子取消活动任务并清空消息，保留 Run 审计、递增游标 |
| `threads.subscribe` | 原版 oRPC SSE；跨多轮保持订阅、断线续传 |
| `agentforge.conversations.list/create/select` | 后端扩展；原版 Rakazo 页面不使用 |
| 分组、归档、分区、定时任务 | 未提供资产时只读返回空集合 |
| models.list / credentials / setDefault | 服务端模型目录、托管路由元数据与按用户保存的默认选择；Bot 可选择目录中的模型 |
| 其余写操作 | 明确返回错误，不伪造成功 |

普通 JSON 与文本消息已支持；附件、语音、Computer、长期记忆、人工审批、分组、独立 Task、前端模型凭据管理等尚未实现。原版入口保留，错误由原有组件处理；不注入统一错误浮层。Board 使用原版 `me.ticketBoardEnabled` 标记保留入口，业务接口仍返回未实现错误。

原版 Skill 浏览和聊天选择的字段映射、权限及版本限制见 [Skill 接入](skills.md#原版-rakazo-web-接入)。个人技能支持 create / update / remove，内置和 Hub 安装副本保持只读；录制示教的 `skills.*` 仍返回 501，Hub 安装复用现有 REST。

## 配置、清空与版本语义

`bots.update` 接受原表单中的未启用默认值（例如 null 的记忆 / 语音 / thinking 字段、false 的 autoSpeak、空禁用工具列表）；启用未实现能力会原子拒绝。模型可以选择后端配置目录中的模型，默认值作用于新建 Bot 或显式重设默认的 Bot；不在此入口保存客户端任意模型凭据。名称或执行配置实际变化才新增 Agent 版本；相同内容或展示字段保存不额外增版。

原版 Bot 设置用于后续运行，因此 RPC send 在事务内读取最新 Agent 配置。已创建 Run 的版本、指令、技能、模型和重试快照均不变；REST 仍使用 Thread 的默认固定版本。无需通过新增前端会话控件让设置生效。

清空使用与 Worker 相同的 Run → Thread 锁顺序，在一个事务内取消排队 / 执行中的 Run、清空 messages 和会话事件正文并追加 `thread.cleared`；不重置事件序号。Run、Attempt 和工具审计保留，可能包含输入输出，清空不是全量数据擦除。若并发 REST 提交抢占会话，可返回 409 而不清除新任务。取消失效的 Worker 不得重新写回。

## 登录与授权

原版 `/sign-in` 使用邮箱密码，POST `/api/auth/sign-in/email`。运维人员通过 `agentforge-admin set-password` 为现有成员配置凭据；邮箱在部署内归一化后唯一，映射明确的租户 / 用户 / 默认空间。密码不出现在参数、配置或日志中，数据库仅保存随机盐 scrypt 摘要。

`/api/auth/get-session`、`/api/auth/sign-out` 与原版 Better Auth 客户端兼容。会话通过 HttpOnly / SameSite=Strict Cookie 传递，HTTPS 时启用 Secure。会话最长 8 小时，逐请求复查成员与令牌授权，密码重设撤销已有密码会话。登录错误不泄露账号是否存在；账户锁定与入口限流见 [本地身份](identity.md)。

公开注册返回 `REGISTRATION_CLOSED`；在线改密、密码找回、账号删除和 SSO 尚未实现，相关请求返回错误。原令牌登录桥 `/api/auth/agentforge` 为兼容工具保留，原版 Web 不调用它。REST 继续仅使用 Bearer；无效 Bearer 不回退 Cookie。

Cookie RPC 与登录写操作要求准确的 `AGENTFORGE_WEB_ORIGIN`；客户端空间选择只允许当前登录空间，不能授予权限。资源默认私有，空间共享不能共享会话选择或聊天历史。

## 请求与事件协议

POST `/rpc/<过程路径>`，例如 `/rpc/threads/send`：

```json
{"json":{"botId":"<Agent UUID>","text":"生成示例报告","clientNonce":"manual-1"}}
```

成功响应为 `{"json":...}`；错误保留相应 HTTP 状态：

```json
{"json":{"defined":false,"code":"NOT_IMPLEMENTED","status":501,"message":"Procedure is not supported by this AgentForge version"}}
```

SSE 的 message 数据也带 json 封装，流内错误使用 error；无 Run 的清空事件省略 runId。会话游标在消息事务内原子递增，独立于 REST Run 游标，Last-Event-ID 优先于 cursor。等待不占用数据库连接；逐条发送前复查授权。当前不是逐 token 流式输出。

## 后端会话扩展

`agentforge.conversations.list` 接收 botId / offset / limit，返回 currentThreadId、agentVersion、current、items、nextOffset。只列当前用户拥有的同 Agent 会话。

create 接收 botId、expectedThreadId、requestId、可选 title；select 接收 botId、expectedThreadId、threadId。新建使用 requestId 去重，当前 / 目标会话必须空闲；客户端前置条件不能授予权限。可选 x-agentforge-thread-id 用于防止旧页面误操作，但原版 Rakazo 不发送该头。

这些是 AgentForge 扩展，不是原版 Rakazo 契约。原版页面运行期间不要通过扩展接口切换当前会话；外部切换后需要重新加载页面。原版订阅没有新增的跨 Thread 游标恢复逻辑，本轮不修改它。

## 数据库与验证

最新迁移 `0011`。`0011` 新增用户模型偏好，不修改历史运行。`0007` 建立 RPC 展示、浏览器会话和消息事件回填；`0008` 增加当前会话选择与无 Run 的清空事件；`0009` 增加密码凭据和浏览器会话关联；`0010` 增加个人技能资产和版本表，有个人版本时拒绝降级。迁移不创建用户或默认密码，不清空已有聊天和 Run。降级 0009 会删除密码凭据并使密码会话失效，保留令牌桥会话与其他业务数据。

```bash
uv run --env-file .env --locked alembic upgrade head
AGENTFORGE_RAKAZO_ROOT=/path/to/rakazo uv run --locked --offline pytest tests/test_orpc_interop.py
```

互通测试直接加载原版 oRPC、Better Auth 与 Zod 契约，通过进程内 HTTP 验证请求、登录、会话、消息、清空和错误；不需要服务端口或真实模型。未配置 Rakazo 路径时明确跳过。浏览器黑盒脚本和手动步骤见 [原版 Web](rakazo-web.md)。

## 流式正文

开启模型快照 stream 后，Worker 的 model_text 事件事务化投影到 thread.progress。使用原版 payload.text / delta / streaming 字段，原版前端无需修改；运行中的 threads.get 返回 progress:<runId> 临时消息，使刷新后可从快照游标续传。成功、失败、取消时去除临时消息，只有最终成功答案写入正式消息。重试用 thread.message.updated 清除旧尝试的草稿；跨租户、空间、用户和撤权检查复用已有策略。事件持久化与边界详见 [模型路由](model-routing.md#可选流式回复)。

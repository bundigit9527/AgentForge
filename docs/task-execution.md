# 任务执行与进度协议

当前实现覆盖模拟任务的提交、去重、持续消费、租约续期、取消和事件续传。已增加 Chat Completions 模式的多轮工具调用与用量事件；真实路由、外部业务副作用及企业登录仍待联调或接入。

## 提交与重试

`POST /api/v1/runs` 保留 `{"prompt":"生成报告"}`，也支持添加 `thread_id` 在已有会话中提交；同一会话只允许一个未结束任务，其他新请求返回 409。客户端可以额外发送 `Idempotency-Key` 请求头；取值为 1–128 个英文字母、数字或 `._:-` 字符。

- 同一租户、空间、用户下，相同键与相同 prompt、thread_id 返回同一个 `run_id`，不再创建任务或入队事件。
- 返回体反映任务的当前状态；首次提交和匹配的重试均返回 HTTP 201。
- 同一键被用于不同任务内容时返回 HTTP 409。
- 不同租户、空间或用户可以独立使用相同键，不会访问彼此的任务。
- 未提供键时，每次提交都是新任务。需要“重新执行”时使用新键。

去重由数据库唯一索引保证，覆盖并发请求。键目前随任务记录保留，不自动过期；此机制只解决提交去重，不代表外部业务操作已经实现幂等执行。

## 实时事件与断线续传

分页查询保持可用：`GET /api/v1/runs/{run_id}/events?after=0&limit=100`。

新增实时接口：`GET /api/v1/runs/{run_id}/events/stream?after=0`，返回 `text/event-stream`。每条持久化事件具有运行内递增的编号：

```text
id: 2
event: running
data: {"sequence":2,"event_type":"running","data":{"attempt":1},"created_at":"2026-10-28T00:00:00Z"}

```

客户端保存最近处理的 `id`，断线后通过 `Last-Event-ID` 请求头或 `after` 参数恢复；两者同时提供时，以请求头为准。事件仍需按编号去重，处理到成功、失败或取消事件后停止主动重连。任务结束且没有剩余事件时，重新请求返回 HTTP 204。

服务端约每 250 毫秒查询一次持久化事件，空闲时每 15 秒发送不含 `id` 的注释心跳。等待期间不持有数据库连接；客户端断开后停止该流的轮询。当前没有模型 token 推送，事件描述任务、工具执行进度及每轮模型用量。`model_completed` 的数据字段、失败码与配置见 [大模型路由](model-routing.md)。

事件流需要与其他任务接口相同的 Bearer 认证。浏览器原生 `EventSource` 无法直接设置此认证头，前端应使用支持请求头的流式 `fetch` 或相应客户端，不能把令牌放在 URL 中。协议依据见 [HTML SSE 标准](https://html.spec.whatwg.org/multipage/server-sent-events.html)。

连接建立前，未授权任务返回 404。连接建立后存储不可用时，流返回不含游标的 `stream_error` 事件并关闭；客户端按已保存游标恢复，不跳过尚未收到的事件。

## Worker 运行模式

保留单次执行：

```bash
uv run --env-file .env --locked agentforge-worker --once
```

需要持续处理任务时，手动使用：

```bash
uv run --env-file .env --locked agentforge-worker --watch
```

`--watch` 按默认 1 秒间隔检查空队列；有任务时连续领取。收到 SIGINT 或 SIGTERM 后停止继续领取，并等待当前同步任务结束；空闲等待可以立即唤醒。它仍是单进程串行执行，一个 Worker 同时处理一个任务。

默认租约为 60 秒，后台心跳每 20 秒续期。可用 `--lease-seconds`、`--heartbeat-interval` 和 `--poll-interval` 调整；心跳间隔必须小于租约时长。

只有仍持有有效执行标识的 Worker 才能续租、开始工具或提交结果。任务取消、过期或被重新领取后，旧执行不能恢复租约或覆盖结果。续租失败后，当前尝试停止产生新的工具操作和最终结果写入，等待租约过期后由其他执行尝试恢复。

取消与租约失效不会强制杀死正在运行的同步 Python 函数；Chat Completions 请求已有网络超时和轮间执行检查，但在途请求不会即时取消。当前恢复允许重做纯内置工具；模型请求可能重发并重复计费，沙箱及外部业务操作仍需独立的中断与副作用核对机制。

## 数据库升级与验证范围

迁移 `0002` 增加提交去重；迁移 `0003` 增加 Agent、会话、消息、执行尝试和运行快照，不清空已有任务或事件；迁移 `0004` 增加工具调用记录；最新迁移 `0005` 增加 Skill 安装副本。旧任务缺少快照的处理见 [Agent 与会话](conversations.md)。已有开发库后续需显式执行：

```bash
uv run --env-file .env --locked alembic upgrade head
```

`/health/ready` 会检查数据库迁移版本，旧结构返回 503。任务行为及迁移已通过 SQLite 离线测试与独立 PostgreSQL 17.11 集成测试；测试使用临时 schema，不连接外部业务库。Worker 在测试进程中执行，常驻进程及真实模型联调仍需单独验收。步骤见 [PostgreSQL 集成测试](postgresql-testing.md)。

## 工具执行事件

工具现在记录 `tool_started`、`tool_completed` 和 `tool_failed`。事件序号仍单调递增，客户端不应假定固定的最终序号。参数、结果、状态查询及恢复边界见 [版本化工具](tools.md)。

# 外部操作台账与审批接口

已接入 Python ChatHarness 的暂停、恢复与原版 Rakazo 审批卡片。仍未开放外部写工具。

## 接口

- GET /api/v1/runs/{run_id}/effects?offset=0&limit=100：查询所有者的操作记录。
- POST /api/v1/runs/{run_id}/effects/{effect_id}/decision：审批或拒绝。

沿用 REST Bearer 身份；查询和审批均仅允许 Run 所有者，分享 Run 不分享审批或参数访问权。
审批还要求当前 admin/member 角色，停用用户或 viewer 不可审批；管理员不能审批他人私有 Run。
请求示例：

```json
{"decision":"approve","request_hash":"从查询响应复制完整的64位request_hash"}
```

摘要绑定工具版本、逻辑目标、参数；变化返回 409。重复相同决定幂等，反向决定返回 409。
已结束或取消 Run 不再接受决定。401/403/404/409 分别表示未登录、权限不足、资源不可见和状态冲突。
没有公开创建操作、发送、完成或强制重试接口；这些能力仅供可信运行时集成。

## 运行时集成契约

ExternalEffectRepository 接收现有 RunRepository：

1. request(claim, operation_key, EffectRequest)：当前租约登记固定请求，返回 pending 记录。
2. 用户通过 REST decide 决定 approve/reject，须提交已审阅的 request_hash。
3. 连接器再次核对当前工具授权、版本与路由，然后 begin_dispatch(claim, effect_id)。
4. 仅成功预占的调用方可发请求；effect_id 可作为远端支持的幂等键。
5. 仅原发送 Attempt 且仍持有有效租约时可 complete；只写已确认成功或失败结果。

operation_key 必须来自固定执行检查点，不能在重试时重新生成随机值或使用变化的模型 call_id。
台账不是工具权限，批准也不允许读取其他用户凭据。target 使用逻辑资源标识，不存 URL、密钥。
调用者必须过滤 request/result 中的凭据及无关敏感数据；接口有大小与结构校验但不承诺自动脱敏。
有审批卡片的请求在批准／拒绝后事务化重新入队；无卡片的独立台账记录仍只更新决定。等待期间释放 Worker 租约，不占用执行线程。

## 不确定结果与取消

begin_dispatch 在网络操作之前提交 dispatched；进程随后即使未发送就崩溃也不会自动重发。
该状态可能代表尚未发送、已发送或远端已成功但未写回，不可推断业务成功或安全失败。
发送后取消/撤权/租约过期会拒绝结果写回，记录保持 dispatched，待后续人工核对机制处理。
本阶段不提供核对后重置或重发接口。取消 Run 不保证远端动作停止。
独立事务记录只能保证本地预占互斥，不能替代远端幂等或结果查询。

## 部署与验证

台账使用 Alembic 0012，完整暂停恢复新增 0013（检查点、审批卡片、放行规则及等待状态）。API/Worker 应统一升级，并按既有流程执行迁移。
本次开发不修改真实数据库或启动服务。未升级到 0013 的数据库会在新 API 就绪检查中提示需要升级。
离线测试覆盖请求绑定、所有者隔离、撤权、并发审批/发送、崩溃重领、取消、结果不可变和迁移保留。
验收结果见本轮交付记录；原版客户端与事件 reducer 有离线互通测试，不能等同于浏览器人工验收。


## 配置并手动验收

部署先执行已有迁移命令（本次开发未对开发库执行）：

```bash
uv run --env-file .env --locked --offline alembic upgrade head
```

API 与 Worker 使用同一版本启动。服务端需配置 Chat Completions 模型；mock 不接受审批配置。
用 REST 创建测试 Agent，设置需要审批的已启用工具版本：

```json
{
  "name": "审批验证助手",
  "instructions": "先调用 example_report 整理用户材料，再根据结果回答。",
  "tools": ["example_report@1"],
  "approval_tools": ["example_report@1"]
}
```

POST /api/v1/agents 使用现有 Bearer 身份。该 Agent 会出现在原版 Rakazo Web 中。
已有 Agent 使用 PUT /api/v1/agents/{id} 保存新版本，须携带 expected_version 和完整配置，
保留已有 tools、skills、mcp_tools，避免意外清除绑定。approval_tools 默认空，旧 Agent 不自动启用。
原版 bots.update 修改其他设置时保留该字段；不得切到不支持审批的 mock 配置。
MCP 工具也可将已绑定的 reference 加入 approval_tools，审批不扩展 MCP 的只读权限。

1. 在原版聊天请求工具整理材料，出现审批卡片时 Run 为 waiting_input。
2. 刷新页面，卡片、具体参数和等待状态仍保留。
3. 选择允许本次，调用已保存的原始工具请求，再将结果交给模型；不会重新询问模型生成该调用。
4. 选择拒绝，工具不执行，拒绝结果交给模型继续回答。
5. 选择始终允许，保存当前用户、空间、精确工具版本的放行规则；其他用户和其他版本不继承。
6. 重复相同回答幂等，反向回答返回 409；等待期间取消或清空后不能再恢复原任务。
7. 历史卡片是消息展示数据，不进入下一轮成功对话上下文。

原版 UI 使用 threads.answer，输入 botId、runId、messageId、answer（allow/always/deny）。
可选 threadId 必须匹配当前会话；不实现密码、语音或任意问答卡片语义。
回答事务同时更新外部操作、卡片、Run 排队状态和事件。REST decision 接口也走同一恢复逻辑。

## 放行规则与运行恢复

- GET /api/v1/approval-grants：当前用户的版本化工具放行规则。
- DELETE /api/v1/approval-grants/{tool_reference}：撤销后续操作的自动放行，不取消已经明确批准的具体操作。
- 原版审批规则管理页面和自动审核服务尚未接入；放行规则可通过上述 REST 接口管理。
- 检查点固定本轮模型消息、待执行工具批次、处理位置和轮次数，大小上限 500000 字符。
- 恢复前仍执行当前权限、租约、工具参数和 MCP 定义检查；批准不能扩大工具权限。
- 结果已记录但检查点未推进时，复用该操作结果；发送后结果未知时以 effect_uncertain 终止，不重发。
- 未审批的纯函数／只读工具仍沿用原有重试边界；模型请求在崩溃窗口内可能重复计费。
- 审批恢复次数与故障重试预算分开，五次正常审批不会触发三次崩溃重试上限。
- 数据库降级前须先处理等待任务；SQLite 表重建后校验全部外键，保留历史 Run、消息和审计。

本功能不等于接入外部写连接器、OAuth、自动审核、Computer 或远端 exactly-once 保证。

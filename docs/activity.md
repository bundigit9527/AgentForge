# 原版 Activity 与工具执行记录

以 Rakazo `d165de43` 为契约基线。API 使用已有 Run / ToolJournal，不增加任务队列、后台执行器或前端组件。数据库版本仍为 0016，本轮不需要迁移。

## Activity 列表

原版 `POST /rpc/runs/list`，输入 `{"json":{"filter":"active"}}` 或 `recent`。

- active 返回 queued、running、waiting_input。recent 返回最近 20 条 succeeded、failed、cancelled，succeeded 映射为 completed；以更新时间和运行 ID 稳定排序。
- 返回 Bot 名称、原输入摘要、状态、时间与 notifyOnFinish。附件独立消息以文件名作为摘要。
- 只查询当前租户、空间、用户拥有的运行，并重新检查成员关系及 Bot 可见性。共享 Run 或 Thread 不扩大此列表范围。
- 原版点击 Activity 只按 Bot 打开当前会话，因此本接口只展示该 Bot 当前选择的会话；扩展 API 切换到其他会话后，对应列表随之切换。无 Bot 的旧 REST Run 不混入此入口。
- 清空会话后不展示旧消息摘要，底层审计 Run 保留。原版搜索、状态和日期过滤在这两组列表上工作，并非全库搜索；独立 Task、Routine、看板没有因此实现。

原版账户设置的 Show tool activity 默认关闭。需要查看步骤时由用户打开；本轮保留原版偏好行为。

## 工具记录

调用开始、结束与 Run 状态变化仍在存储事务中提交。使用工具台账中的名称、版本、状态和起止时间生成原版消息块，不复用模型生成的执行声明。

- 执行中用 meta 展示工具名称；成功调用生成 steps，可合并相邻同名工具的次数，durationMs 为这些成功调用的实际执行时间之和，不含模型推理与审批等待。
- 失败用 meta 展示公开错误码；取消、租约过期或运行结束时仍未完成的调用显示“结果未确认”。原版 steps 总是显示成功勾选，因此这些状态不放入 steps。
- thread.message.updated 更新运行中 progress 消息；threads.get 从同一游标以内的事件恢复活动及模型草稿。最终消息保存工具步骤，失败或取消有单独活动消息。刷新、重连后仍可查看。
- 重复工具请求命中缓存不会重复添加步骤；失效 Worker 无权更新状态。审批未通过或尚未执行的工具不会被描述为已经执行。
- 工具参数、原始结果、凭据和异常栈不放进活动摘要。需要查看详细调用时，已有 REST tool-calls 接口继续按资源权限控制。
- 仅新产生的终态消息持久化此投影；升级不重写之前的聊天事件。历史 Run 的原始工具审计记录仍在。

验证覆盖 SQLite、PostgreSQL 的事务与隔离、原版 oRPC/Zod 契约及前端 reducer 回放。浏览器黑盒脚本见 `tests/interop/rakazo_activity.mts`，只连接显式启动的临时测试服务，不读取开发库或调用真实模型。

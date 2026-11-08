# Python Harness 上下文管理

本轮保留 Python API、Worker 与 Harness，不增加 Node.js、Pi、数据库表或后台服务。策略用于避免较早的聊天历史挤占当前任务空间；它不是语义摘要或长期记忆。

## 配置与兼容

```dotenv
AGENTFORGE_MODEL_CONTEXT_POLICY=trim_history
AGENTFORGE_MODEL_CONTEXT_MAX_CHARS=100000
```

- `trim_history` 是新真实模型配置的默认策略。预算范围为 4000–200000，默认 100000。
- 计数对象为 `messages` 与 `tools` 的紧凑 JSON（不转义 Unicode）的字符数，包括 JSON 结构和转义字符；不是 UTF-8 字节数或模型 token 数，也不包含 model 等其他请求参数。不能据此承诺适配任意模型的 token 窗口；输出 token 上限仍由 MAX_COMPLETION_TOKENS 单独控制。
- 个人模型可同时固定 `context_max_bytes`，按 `messages` / `tools` JSON 的 UTF-8 字节数控制请求体预算。任一上限触发时均按完整历史轮次裁剪，裁剪后必须同时满足字符与字节上限；字节上限不是 token 上限。
- `reject` 不裁剪历史，保留原有 messages JSON 计数方式及 model_limit_exceeded 错误；其计数不包含工具定义，是历史兼容策略。
- 缺少新字段的旧 Agent / Run 快照默认 `reject`、100000。默认配置变化不会改写已有 Bot、排队任务或重试输入；Worker 依据快照解析策略和预算。
- 模拟模型不理解历史，保持 reject 策略；这一功能面向真实 Chat Completions 循环。
- API 与 Worker 重启后读取配置。已有 Bot 需要在原版模型设置中保存模型选择以产生新版本；新建 Bot 使用新的默认配置。部署必须让 API / Worker 使用一致代码版本，旧版进程无法读取含新增字段的快照。本轮只更新代码和模板，未改本机 .env、Bot 或运行服务。

## 请求前处理

1. Run 提交时仍固定最近 20 个成功完整轮次；失败 / 取消的输入不会进入成功历史。
2. 每次模型请求前，计算当前指令、Skill 目录、历史、当前问题、工具定义和本轮工具交互的大小。
3. 超过预算时从最早的历史 user / assistant 对开始移除，直到请求可容纳；优先保留近期完整轮次，不保留孤立回答。
4. 插入一条固定说明，告知模型部分早期历史已省略，不应声称记得这些细节。这条说明也计入预算，不保证模型一定遵循。
5. 指令、Skill 目录、当前问题、工具参数和本轮工具结果保持完整。若全部历史移除后仍超限，返回 model_context_limit_exceeded，不发送下一次模型请求。已经完成的工具执行记录仍保留。

裁剪仅影响本次执行内的历史副本，不修改聊天消息、Agent 配置或 Run 的 context 快照。重试使用原有输入并重新执行模型循环；同样的模型回复产生同样的裁剪选择，模型回复变化时后续裁剪也可能变化。摘要、磁盘记忆、文件检索和从中间工具位置恢复尚未实现。

## 审计与权限

成功选出更短上下文后，Worker 通过当前租约写入 context_trimmed，再检查执行状态并调用模型。事件含 attempt、step、budget_chars、before_chars、after_chars、dropped_turns、retained_turns；快照含字节预算时还记录 budget_bytes、before_bytes、after_bytes。仅字节超限也必须接受裁剪记录，不能要求 before_chars 必须大于字符预算。缺少字节字段的历史事件保持兼容；不记录被省略正文。原有 Run 审计和消息仍可能包含原文，裁剪不等于数据删除。

取消、过期执行、撤权和旧 Worker 均不能写入新裁剪事件。事件写入成功不代表模型调用已经发生，两者不属于同一事务。REST Run 事件查询 / SSE 可读取统计；不修改原版 Rakazo UI 或增加私有会话事件类型。

## 本地验证与断点

无需服务或真实模型：

```bash
uv run --locked --offline pytest tests/test_context_budget.py -q
```

断点建议：`ContextWindow.fit` 查看历史选择与 before / after 大小；`ChatHarness.execute` 查看发送的消息和完整工具链；`RunRepository.record_context` 查看执行租约与审计。

手动联调时，先在本机 .env 将预算暂设为 4000–7000，按原有命令重启 API / Worker，然后创建新 Bot 或保存现有 Bot 的模型设置。分多轮提供较长材料，逐轮核对 REST Run 事件中的 context_trimmed 和最终回答；聊天历史应仍完整保留。单次输入本身超过预算则应失败，不能期待通过删历史解决。测试后恢复期望预算，并为 Bot 保存新版本；之前提交的任务保持旧配置。

验证范围：离线 HTTP 模拟、SQLite 事务和原版 oRPC 客户端互操作。语义回答质量、真实路由 token 限制、生产容量和 PostgreSQL 专项回归需独立验收。

## 中文联网任务修复（2026-11-08）

曾出现搜索及两次网页读取均成功、随后返回 execution_failed 的问题。原任务记录显示：19,090 个字符尚未超出 100,000 字符上限，但 UTF-8 请求上下文达到 29,949 字节，超过 28,672 字节预算。Harness 已选出合适的历史轮次，存储层却按“字符必须超限”拒绝记录，导致 Worker 中断。

现在裁剪选择和事件写入分别核对两种单位；当前问题与本轮工具结果保留，旧消息和 Run 快照不改写。原始工具结果的临时 Worker 重放在第 3 次模型调用前正常记录裁剪，省略 5 个旧轮次后为 27,936 字节；同一原始问题、26 条历史和个人模型的真实 Worker 验证亦完成搜索与网页读取并返回结果。某个网页的大小限制仍保留为失败工具结果，不伪造读取成功。开发会话未写入验收消息。

回归覆盖字节单独超限、非法统计、取消 / 租约失效 / 撤权，以及个人模型在两次网页读取后继续处理的完整 Worker 路径。当前仍是完整轮次裁剪，尚未实现 Rakazo 的历史检索、旧工具正文缩减或语义摘要。

本次修复验证：SQLite 731 项通过、12 项环境相关跳过；独立 PostgreSQL 743 项通过（含原版客户端互操作）；Ruff 检查通过。临时数据库已关闭，不修改开发库。

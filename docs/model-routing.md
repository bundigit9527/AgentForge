# 大模型路由与工具循环

已提供 OpenAI 兼容 Chat Completions 接口适配器。默认仍为 `mock`，保持原有离线行为；只有任务快照选择 `chat_completions` 且 Worker 配置匹配路由时才访问模型。当前通过模拟 HTTP 响应验证协议与任务流程，真实企业路由的地址、模型能力与凭据仍需联调确认。

## 工作流程与实现位置

Worker 领取任务 → 检查执行租约 → 请求模型 → 记录模型用量事件 → 校验工具及参数 → 执行内置工具 → 把工具结果送回模型 → 保存最终回答。

模型可以直接返回最终回答，也可以连续选择工具。一次响应中的所有工具先完成校验，再串行执行；未知工具、额外参数、空文本和重复调用 ID 会终止本次任务。

| 文件 | 职责 |
| --- | --- |
| `packages/runtime/agentforge_runtime/config.py` | API / Worker 模型配置、快照与路由校验 |
| `packages/runtime/agentforge_runtime/chat_model.py` | HTTP 请求、响应校验、用量解析和错误分类 |
| `packages/runtime/agentforge_runtime/chat_harness.py` | 多轮工具调用、上下文和轮次限制、执行检查 |
| `apps/worker/agentforge_worker/main.py` | 按环境配置创建、注入并关闭模型客户端 |
| `packages/storage/agentforge_storage/repository.py` | 按有效租约记录模型事件与任务结果 |

当前默认选择 `example_report@1` 工具，也可为 Agent 选择 `text_stats@1` 或空工具列表，详见 [版本化工具](tools.md)。报告工具的参数为 `{"text":"需要整理的文本"}`。工具把文本包装为报告草稿，结果回传模型用于最终回答；它不访问网络、不执行命令、不创建文件，也不进行事实核验。已绑定 Skill 时额外提供内部 skill_read，按需读取本次快照，见 [Skill 安装](skills.md)。其他业务工具及企业系统接入按后续阶段实现。

## 配置

依赖中已将原有 `httpx` 调整为运行依赖；首次切换到本版本后执行 `uv sync --locked`。API 读取模型协议、地址、名称和限制以生成快照；只有 Worker 读取模型密钥。

在本地 `.env` 中合并下列字段，保留已有数据库和认证配置：

```dotenv
AGENTFORGE_MODEL_PROVIDER=chat_completions
AGENTFORGE_MODEL_BASE_URL=https://your-gateway.example/v1
AGENTFORGE_MODEL_NAME=your-model-name
AGENTFORGE_MODEL_API_KEY=
AGENTFORGE_MODEL_TIMEOUT_SECONDS=30
AGENTFORGE_MODEL_MAX_ROUNDS=8
AGENTFORGE_MODEL_MAX_COMPLETION_TOKENS=2048
```

填入实际地址、模型名和本地密钥后才能请求真实路由；不要提交密钥。模板 `.env.example` 默认保留 `mock` 和空模型配置。真实模式配置缺失或无效时，在领取任务前报错，不自动回退到模拟结果。

- `BASE_URL` 是接口前缀，不含 `/chat/completions`；客户端会自动追加此路径。
- 请求使用 Bearer 认证、`messages`、`tools`、`tool_choice=auto`、`n=1`、`stream=false` 和 `max_completion_tokens`。路由与所选模型需要支持这些字段和工具调用。工具列表为空时省略 tools 与 tool_choice。
- 地址不能包含账号密码、查询参数或 fragment。生产路由应使用 HTTPS；HTTP 只适合明确受控的开发或内网环境。
- 客户端不跟随重定向、不自动重试、不继承环境代理配置。需要代理或特殊认证时应通过明确的适配实现。
- 连接、读写等网络阶段超时默认为 30 秒，允许 `(0, 120]`；响应读取循环也检查累计时间。同步网络 I/O 受各阶段超时约束，不承诺严格的单请求总耗时上限。
- 每个任务最多 8 轮模型请求，可配置为 1–32；每轮最多返回 8 个工具调用，按序执行。达到最后一轮仍要求工具时直接失败，不执行无法再回传结果的工具。
- 每次请求的生成 token 上限默认 2048，可配置为 1–8192；另限制响应体为 1 MiB、单条文本为 20000 字符、工具参数文本为 10000 字符、上下文 JSON 为 100000 字符。

数据库与 API 已按 [开发入门](development.md) 配置好，且已授权模型联调时，使用：

```bash
uv run --env-file .env --locked agentforge-worker --once
```

持续运行仍使用 `--watch`。默认配置在进程启动时读取，修改后重启对应 API / Worker。实际执行使用 Run 快照；不同默认模型的 Worker 只要配置同一路由，即可按快照执行，路由不匹配时失败。当前尚未按路由筛选队列，应为共享队列配置一致的路由。

直接构造 `Worker(repository)` 只服务模拟快照，遇到真实路由快照会失败。生产入口按任务调用 `configured_harness(settings.for_snapshot(snapshot.model))`；该上下文管理器负责关闭客户端。测试可注入与快照模型配置一致的 Harness。

## 事件与错误

真实模式的每轮有效响应产生 `model_completed` 事件，例如：

```json
{
  "attempt": 1,
  "step": 1,
  "model": "your-model-name",
  "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
}
```

事件沿用现有单调游标和 SSE 续传机制，不保存上游响应原文、请求头或密钥。`model` 使用 Run 快照中的模型名称；上游未返回用量时保存 `usage: null`，不把未知用量当作零。可以按执行尝试和轮次汇总已记录的用量，但这不是完整计费账本：超时、取消、无效响应或进程中断可能已经产生上游费用而未能记录。

| `error_code` | 含义 |
| --- | --- |
| `model_timeout` | 网络阶段超时或响应读取超时 |
| `model_auth_failed` | 路由返回 401 / 403 |
| `model_rate_limited` | 路由返回 429 |
| `model_unavailable` | 连接错误、重定向或其他非 200 响应 |
| `model_invalid_response` | 响应结构、用量、结束原因或调用 ID 不合法 |
| `model_refused` | 路由明确拒绝或过滤内容 |
| `model_limit_exceeded` | 达到轮次、上下文、响应大小或生成长度限制 |
| `tool_not_allowed` | 请求了未授权工具 |
| `tool_invalid_arguments` | 参数类型、字段、长度或内容不符合工具定义 |
| `tool_execution_failed` | 工具执行异常 |
| `tool_invalid_result` | 工具结果不符合契约或大小限制 |
| `tool_call_conflict` | 同一执行尝试中的调用标识冲突 |
| `model_configuration_mismatch` | Worker 路由或注入的 Harness 与快照不匹配 |
| `execution_config_missing` | 升级前的任务缺少可验证执行配置 |
| `execution_failed` | 其他执行异常 |

任务仍使用既有 `failed` 状态与 `error_code` 字段；会话与快照阶段新增迁移 `0003`，工具记录阶段新增 `0004`。上游错误正文不会写入任务结果或日志。已明确失败的任务不自动重发 HTTP 请求，需要重新执行时提交新任务；幂等键复用规则保持不变。

## 取消与恢复边界

每轮模型请求前后、工具调用前、模型事件和最终结果写入时都检查执行状态或有效租约。取消、租约到期、被重新领取或续租失败后，不继续发起新的调用，也不覆盖最终状态。

已发出的同步 HTTP 请求不会因取消立即终止，上游可能继续生成并计费；返回后丢弃失效结果。租约检查与外部请求之间也不是分布式原子操作。Worker 崩溃恢复目前从任务开头重新执行，可能再次请求模型；它不提供模型请求的 exactly-once 保证，不能据此自动重放具有业务副作用的工具。

本版仍是非流式模型响应，SSE 展示任务、工具和用量事件，没有逐 token 输出。已支持持久化会话与最近成功消息上下文；长期记忆、检查点恢复、真实模型中断和外部业务工具的幂等将在后续实现。

## 验证

```bash
uv run --locked --offline pytest tests/test_model_configuration.py tests/test_model_gateway.py tests/test_model_worker.py
uv run --locked --offline pytest
```

模型测试使用 HTTPX MockTransport，不读取本地 `.env`、不连接真实路由、不消耗模型额度。数据库用例可复用 [PostgreSQL 集成测试](postgresql-testing.md) 模式。

真实路由验收需另外确认：支持工具调用的模型名、接口前缀、Bearer 凭据、TLS 与网络可达性，以及至少一次“工具调用 → 工具结果 → 最终回答”的结果和用量。代码测试通过不代表这一步已经完成。

接口依据：[Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 与 [Function calling](https://developers.openai.com/api/docs/guides/function-calling)。

## 模型路由阶段验证记录（2026-10-29）

- SQLite：138 项通过，5 项 PostgreSQL 专用用例按预期跳过。
- 独立 PostgreSQL 17.11：143 项通过，包含模型事件持久化、失败码、取消与租约失效回归。
- Ruff 与格式检查通过，第三方依赖版本保持不变；HTTPX 从开发依赖移入运行依赖。
- PostgreSQL 临时 schema 残留为 0，测试容器和网络已清理。
- 模型请求全部使用 MockTransport，未配置或访问真实企业路由；真实路由联调尚未完成。
- 保留一条既有 Starlette / httpx 弃用提示，不影响测试通过。

## Agent 与会话阶段的配置变化

API 现在也需要模型协议、地址、名称和限制，用于在提交时生成不可变快照；API 不读取模型密钥，密钥只需配置在 Worker。Agent 创建和更新保存当时配置，会话固定 Agent 版本。独立 Run 保存 API 当前默认配置；会话 Run 使用绑定版本的配置。

Worker 按 Run 快照选择模型和限制，使用本地当前凭据。更换默认模型不会改变旧 Run；路由不匹配返回 `model_configuration_mismatch`，没有快照的旧 Run 返回 `execution_config_missing`。不会静默切换为模拟模式。程序内注入的 Harness 也必须与快照模型配置一致。

消息历史、版本更新和升级行为见 [Agent 与会话](conversations.md)。本阶段仍无 token 流、长期记忆或外部业务工具。

加入会话与快照后的完整回归为 SQLite 167 项通过、5 项跳过，PostgreSQL 172 项通过。详见 [集成测试记录](postgresql-testing.md)。

工具框架阶段回归：SQLite 196 项通过、5 项跳过；PostgreSQL 201 项通过。工具执行路径与查询接口见 [版本化工具](tools.md)。

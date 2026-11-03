# 与 Rakazo 对齐的架构基线

更新日期：2026-11-02。参考已核查的 Rakazo 本地版本 `d165de43`（2026-10-15）；不代表远程最新版本。

AgentForge 以 Rakazo 的服务职责、执行链路、资源边界和恢复语义作为参考，以 Python 实现后端。现有企业系统接入、资产版本快照和 MIT 项目方向保留；已选择 Python 后端兼容 Rakazo oRPC，保留原有 REST；React / Electron 的完整页面和能力入口仍需联调。

## 本轮代码核查：对齐情况与实际差距

结论：API → Worker → Harness → Model / Tool 的职责方向没有改变；AgentForge 当前是可运行的 Python 核心与原版 Web 兼容子集，不能按 Rakazo 已有的页面数量判定功能齐全。以下为本地源码对比，不代表已对 Rakazo 全部功能重新运行验收。

| 维度 | Rakazo 本地原版 | AgentForge 本轮完成后 | 判断 |
| --- | --- | --- | --- |
| 客户端与协议 | React / Electron 等客户端使用 oRPC 与 Better Auth | 原版 Web 源码不变，Python 兼容登录、聊天、设置及 Skill；桌面未验收 | 复用方向对齐，接口覆盖仍有缺口 |
| 执行与队列 | TypeScript Worker、Graphile Worker、Pi 运行时 | Python Worker、数据库任务队列、Harness 与 Chat Completions | 职责对齐，语言和队列是有意差异 |
| 上下文与恢复 | Pi 流式输出、压缩、暂停 / 恢复等实现 | 最近 20 轮成功上下文、文本流、字符预算与完整历史轮次裁剪、Run / Attempt、租约和重试；无语义摘要或完整检查点 | 尚未实现的运行能力 |
| Skill | 内置 / plugin / user 目录；用户 Skill 在同一资产行编辑 | 内置 / Hub 安装 / 个人 Skill；原版创建编辑删除接通，资产 ID 稳定、内容版本独立保存 | 主要操作对齐；不可变版本与 HTTP Hub 是企业扩展 |
| 模型 | 模型目录、连接、凭据与备用模型等接口 | 后端环境变量配置单一路由，Chat Completions 已适配；真实路由待联调 | 下一项接入重点 |
| 身份 | Better Auth 账号和会话体系 | 本地邮箱密码、租户 / 空间角色、令牌、共享；SSO / 注册找回未完成 | 登录协议已兼容，企业身份仍待接入 |
| Task / Run / 外部动作 | 独立 Task / Run / ExternalEffect 等实体与流程 | Run / Attempt 已实现，taskId 仍是兼容标识，审批和外部动作记录未实现 | 不能把 tool_calls 视为外部动作恢复能力 |
| Computer、记忆、文件 | SandboxProvider / Docker、MemoryProvider、文件相关实现 | 仍为规划，现有消息历史不等于长期记忆 | 主要功能缺口 |
| 交付与验证 | 多客户端和部署相关代码 | 本机开发流程和离线测试；Ubuntu 应用镜像、备份恢复及生产验收未完成 | 不能据此认定已可生产交付 |

核查依据：Rakazo 的 `apps/worker/src/index.ts`、`packages/adapters/src/pi-runtime.ts`、`packages/adapters/src/approval-effect.ts`、`packages/adapters/src/sandbox-factory.ts`、`packages/adapters/src/memory-provider-factory.ts`、`apps/api/src/agent-skills.ts`、`packages/contracts/src/rpc.ts` 和 `packages/db/prisma/schema.prisma`；AgentForge 的 `apps/api/agentforge_api/orpc.py`、`packages/runtime/agentforge_runtime/chat_harness.py`、`packages/storage/agentforge_storage/repository.py`、`packages/storage/agentforge_storage/user_skills.py` 及相应测试。

本轮优先补齐个人 Skill 编辑，是因为它可以直接复用原版页面并形成“创建 → 选择 → 执行 → 编辑 → 再执行”的完整流程。没有通过修改 Rakazo 页面掩盖后台缺口；未接通的 Hub 管理、示教录制及模型连接入口仍返回原协议错误。

## 模块映射与现状

| Rakazo 概念 | AgentForge 对应 | 当前状态 |
| --- | --- | --- |
| API / 身份与资源服务 | FastAPI / contracts / storage | 已实现本地身份、空间成员角色、令牌与显式共享；企业登录待接入 |
| oRPC / Better Auth 客户端 | Python RPC 适配层 / 本地浏览器会话 | 原版源码保持不变；邮箱密码登录、核心聊天、设置和清空兼容在 Python 后端实现；SSO 与完整产品功能未验收 |
| Worker / Run Executor | Python Worker / 任务存储 | 已有领取、续租、失效保护、Attempt；恢复语义待补齐 |
| Pi AgentRuntime | Python Harness / ChatModel | 已有模型和工具循环、技能快照、文本流及历史预算裁剪；语义摘要和检查点待补 |
| Bot / Thread / Message | Agent / AgentVersion / Thread / Message | 已实现；版本固定为企业扩展 |
| Task / Run / Attempt | Task / Run / Attempt | Run、Attempt 已实现；独立 Task 待工作流阶段增加 |
| Builtin / AgentSkill / skill_read | 内置 / 安装 / 个人 Skill 及 skill_read | 已实现原版 CRUD、不可变版本和按需读取；Hub 安装是企业扩展 |
| ConnectorProvider / adapters | core 中的接口 / connectors 中的协议适配 | 已有 SkillHub 接口与 HTTP 适配；业务连接器待补 |
| ExternalEffect / approval checkpoint | 外部操作记录 / 审批 / 运行检查点 | 待实现；现有工具记录不可替代 |
| SandboxProvider / Supervisor / Computer | Python 环境接口 / Supervisor / Docker Computer | 目标边界一致，尚未实现 |
| MarkdownMemoryStore / MemoryProvider | Memory 模块与企业知识连接器 | 尚未实现，现有消息历史不等于长期记忆 |
| ArtifactStore / AgentHome | 文件产物与工作目录存储 | 尚未实现 |

Rakazo 代码参考：`apps/worker/src/index.ts`、`packages/adapters/src/executor.ts`、`packages/core/src/agent-skill.ts`、`packages/adapters/src/skill-tools.ts`、`packages/adapters/src/approval-effect.ts`、`packages/adapters/src/sandbox-factory.ts` 和 `packages/db/prisma/schema.prisma`。

## 必须保持的边界

- API 负责身份、资源、提交和事件；耗时 Agent 决策位于 Worker 中。
- Worker 承担执行协调；Harness 负责模型、上下文和工具循环。模型及 Skill 正文不决定访问权限。
- Skill 是操作方法和验收要求，Tool 是具体动作。先提供可用目录，再由 skill_read 读取选定技能；内置与安装技能共用读取路径。
- 上游广场是资产来源，本地保存明确版本。Hub 通信限于 API 的连接器；Worker 执行安装副本，不在运行中追随远程 latest。
- 工具请求统一经过权限、校验和执行记录；未来外部写操作须补审批、独立幂等键与不确定结果处理，不从模型请求重放推断成功。
- Computer 只执行脚本、文件和浏览器动作；只有 Supervisor 管理 Docker。API、Worker 和 Computer 不持有 Docker socket。
- 记忆、知识与文件独立持久化，并按资源权限访问。共享 Worker 或同一 Bot 目录树不构成强隔离。

## 明确保留的差异

- `/rpc` 覆盖 bots 与 threads 的核心聊天子集，JSON / SSE 格式经过原版客户端验证；不等于复制全部 oRPC 功能或所有 Rakazo 产品接口。详情见 [兼容范围](rakazo-orpc.md)。
- 当前 `taskId` 是 Run 的稳定兼容标识，不能据此宣称已有独立 Task；RPC 会话按用户隔离，提交时采用最新 Agent 设置；每个 Run 固定版本，REST 会话仍按创建时版本运行。
- Python 技术栈；首版数据库任务表兼任队列，不为复刻 Graphile 引入另一套组件。
- Agent 和运行配置固定版本；Hub 安装固定来源、版本和内容摘要。升级技能需要新 Agent 版本，旧 Run 和 REST 会话不静默变更；原版 RPC 下一次提交使用最新 Agent 配置。
- Agent 可显式绑定技能，原版 Web 也可通过用户可见目录和 `/技能名` 为单次 Run 选择技能。个人技能创建 / 编辑 / 删除已接通；Hub 管理页面和录制示教体验未覆盖；选择不自动授予工具权限。
- 个人技能支持受限的 SKILL.md YAML 导入、编辑和删除，保留未知元数据为普通数据。Hub 与内置版本保持只读；资源包、可执行资源及模型侧 skill_create / skill_update / skill_delete 工具未实现。
- 不复制 Rakazo 的所有产品模块；移动端、语音、外部聊天渠道等未列为当前交付范围。

## 后续验收顺序

1. 保持 Rakazo 原版 `d165de43` 源码；登录、设置、清空与协议映射在 AgentForge 实现。已撤回前端专用会话选择、版本提示、登录分支和统一错误浮层。已补原版 Skill 目录、个人技能 CRUD 和聊天选择；后续优先补模型目录 / 路由接口、真实路由与企业登录，禁止通过新增 Rakazo 专用控件解决后端能力差异。共享授权不替代外部业务审批。
2. 审批暂停/恢复、外部操作记录与检查点；验证崩溃后不盲目重复业务操作。
3. Supervisor / Docker Computer / 文件产物；验证授权工作目录、资源限制、取消与回收。
4. 接入已有广场和知识系统，并增加 Task 与简单工作流步骤；完成“取资料 → 分析 → 报告 → 人工确认”。工作流在执行核心之上编排，不替换 Harness。
5. 最小 Web / 桌面交互及 Ubuntu 容器化交付，完成监控、备份与恢复演练。

此处是实施基线和差距清单，不表示所有 Rakazo 能力已经完成。

### 2026-11-02：聊天模型选择与 DeepSeek 适配

原版 Web 的 models.list / credentials / setDefault 和 Bot 模型选择映射到同一服务端路由。用户默认值按租户、空间、用户隔离，运行继续固定模型快照。DeepSeek V4 Pro 使用官方 Chat Completions 普通模式，工具执行仍在 Python Harness。没有修改 Rakazo 源码，也没有引入第二套执行循环；个人模型密钥管理、思考模式、逐 token 输出仍未实现。DeepSeek 官方账号已在临时 SQLite 库通过真实验收：第一轮工具调用与结果返回、第二轮上下文引用，共 3 次模型请求；未启动常驻服务，未修改开发数据库。

### 2026-11-02：保留 Python Harness，增加上下文预算

用户确认优先完善纯 Python 执行引擎，暂不引入 Pi / Node.js 执行进程。已实现配置快照中的字符预算、按完整轮次裁剪历史、context_trimmed 审计及本轮工具链保护。该实现减少可避免的历史超限失败，但没有复刻 Pi 的全部上下文压缩、暂停恢复和扩展能力；模型本身的 token 限制仍需按路由配置。Rakazo 源码不变。

### Python MCP 远程工具

新增 Streamable HTTP 工具发现、REST Agent 绑定、不可变工具定义、运行前定义核对、作用域授权和统一审计。原版 bots.update / threads.send 可保留并执行已经绑定的 MCP 工具，Skill 与 MCP 的工具循环已验证。原版 Rakazo 源码不变；MCP 管理页面、OAuth、本地 stdio、resources / prompts、写工具审批仍未实现，不能据此宣称整个连接器体系已对齐。


## 2026-11-03：外部操作与审批台账（基础阶段）

已新增迁移 0012、ExternalEffectRepository、运行所有者专用的 REST 查询与审批接口。
请求固定工具版本、逻辑目标和参数摘要；同一 Run 的 operation_key 只能绑定相同请求。
只有当前有效租约持有者可以登记、预占发送和完成操作；审批需当前写权限及请求摘要匹配。
每次状态变化和 effect_updated 事件在同一事务提交，事件中不放操作参数或结果。

状态为 pending → approved / rejected → dispatched → succeeded / failed。
dispatched 表示已预占发送、结果尚未确认，不证明远端执行成功。
崩溃发生在发送前、发送中或结果写回前，均不能自动重放该记录；failed 也不能直接重试。
该机制实现本地发送预占，不能保证远端 exactly-once；具体连接器仍须使用幂等键和查询接口核对结果。

本阶段尚未接入 Harness 的审批暂停与检查点恢复，现有工具不会自动创建审批记录。
没有开放写工具、修改 Run 状态机或接通 Rakazo 审批卡片；不能把台账接口描述为完整审批流程。
下一步应在固定工具请求与工具结果的执行检查点之上实现暂停、恢复与原版 UI 协议映射，
再接入经过明确授权的外部写连接器。参见 [外部操作台账](external-effects.md)。


## 2026-11-03：审批暂停与原版聊天恢复

已完成 Python ChatHarness 固定工具批次检查点、waiting_input 暂停与租约释放、原版 ask 卡片和
threads.answer。支持允许本次／始终允许／拒绝、刷新恢复、重复回答幂等、撤权及取消拦截。
审批恢复重新排队同一 Run，使用固定参数继续工具循环；已确认结果可复用，未知操作不自动重发。
版本化 approval_tools 默认空，通过 REST Agent 配置启用，仍仅允许现有纯函数和受控只读 MCP。
迁移 0013 尚未应用到开发库；API 和 Worker 需统一升级。放行规则提供 REST 查询与撤销，
原版规则管理页、自动审核和外部写连接器尚未实现。详见 [审批说明](external-effects.md)。

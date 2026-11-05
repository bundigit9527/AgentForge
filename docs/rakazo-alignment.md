# 与 Rakazo 对齐的架构基线

核查日期为 2026-11-04。比较对象为本地 Rakazo 原版 `d165de43` 与 AgentForge 迁移 `0016` 对应的实现。Rakazo 一侧按源码核对，AgentForge 一侧区分了本机联调、自动化回归和待验收能力。

## 本轮代码核查：对齐情况与实际差距

| 能力 | Rakazo 原版实现 | AgentForge 当前实现与差距 |
| --- | --- | --- |
| Web 与身份 | React 客户端通过 Better Auth 和 oRPC 访问服务 | 原版 Web 可按部署开关注册、登录、改密并撤销其他浏览器会话、选模型、多轮聊天、接收 SSE 和回答审批。FastAPI 在后端兼容协议，保留 REST。本地账号、角色和空间授权已实现，SSO 和部分管理接口仍待接入。 |
| 运行与模型 | Graphile Worker 调度任务，Pi 负责 Agent 循环、上下文和工具 | Python Worker 使用数据库队列和租约，Python Harness 调用 Chat Completions。已联调 DeepSeek，支持版本快照、文本流和完整轮次裁剪。缺少 Pi 式语义压缩与独立业务 Task，RPC 的 taskId 为兼容标识。 |
| Skill 与 MCP | 内置及用户 Skill、MCP 连接和工具管理 | 内置 Skill、HTTP Hub 安装、个人 Skill CRUD 和运行时按版本读取已接通。远程 MCP 工具需由部署方登记并限制作用域，原版广场管理和示教录制仍有缺口。 |
| 审批与恢复 | ExternalEffect、审批卡片和回答后恢复执行 | Python Harness 保存工具参数与检查点，审批后恢复同一次操作。已确认结果可复用，已发出但结果未知的操作停止自动重发。通用外部业务写入仍待接入。 |
| Docker 交付 | 生产 Compose 可选 Docker 沙箱覆盖配置，由 Supervisor 操作 Docker Engine | 原版 Web、FastAPI、Worker、Supervisor 分开运行，对接外部 PostgreSQL。API 与 Worker 没有 Docker socket。已验证单机 Colima 部署，目标 Ubuntu 环境待验收。 |
| Computer 分配 | Team 按空间复用，Private 按 Bot 独占。常规 Run 在进入 Pi 前调用 provisionComputer | Team 按租户和空间复用，Private 按 Bot 分配。新 Bot 默认 Team，迁移 0015 将已有 Bot 保留为 Private。普通聊天无需启动桌面，点击启动时才分配或复用容器。 |
| 桌面生命周期与文件 | 默认空闲阈值为十分钟，满足条件后保存工作区并挂起。提供终端及文件接口 | 可观看、接管和交还。Files 可列目录、预览、下载，持有控制权才可上传。Team 的 Files 默认进入各 Bot 目录，但桌面和终端共享。Compose 桌面池在正常停止时归档 /workspace，启动时按原作用域恢复；单次桌面最长四小时。独立 Terminal 可在接管期间使用交互 Shell，与 Files 使用同一 Bot 目录。Compose 支持按空闲时间挂起，停机可列目录及预览文本；跨主机恢复仍待现场验收。 |
| 浏览器 | 通用浏览器工具提供导航、观察和页面操作 | 财经新闻支持后台 RSS，也支持在可见 Chromium 中加载中新网财经列表、读取标题时间、滚动并点击原文。用户接管时 Agent 停止操作。可见流程仅开放指定站点和动作。 |
| 产物与记忆 | 文件产物、版本和 MemoryProvider | 经审批的 CSV 可持久化，在聊天中显示文件卡片，并可由所有者下载。Bot 聊天支持上传、仅附件发送、本轮按授权读取及生成文本结果文件。每个文件目前为独立版本；同名文件版本管理、长期记忆与企业知识检索待实现。 |

Rakazo 对照入口为 `apps/api/src/router.ts`、`apps/worker/src/index.ts` 和 `packages/adapters/src/` 下的 `pi-runtime.ts`、`approval-effect.ts`、`sandbox-factory.ts`、`browser-tools.ts`。AgentForge 对应入口包括 [oRPC 路由](../apps/api/agentforge_api/orpc.py)、[Python Harness](../packages/runtime/agentforge_runtime/chat_harness.py)、[桌面代理](../apps/api/agentforge_api/desktop.py)、[财经网页工具](../apps/supervisor/agentforge_supervisor/visible_news.py) 和 [文件产物存储](../packages/storage/agentforge_storage/artifacts.py)。

**0015 验收范围。** 使用独立 PostgreSQL 通过全部 566 项回归；SQLite 通过 561 项，其余 5 项为 PostgreSQL 专项。真实 Compose 验收通过原版 Web 代理完成双账号密码登录、Team 接管互斥、按 Bot 访问文件、noVNC 页面访问、切换 Private、撤销旧屏幕链接及切回 Team。两个 AgentForge 镜像的本机验收架构为 `linux/arm64`。目标服务器的架构、外部数据库连接和 HTTPS 域名仍需现场验证。[镜像与交付步骤](../infra/delivery/README.md)

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

以 Rakazo 原版 `d165de43` 为功能基线，在 AgentForge 后端实现兼容，用原版 Web 与两名用户验收。当前起点为 `aee6043`。下表记录各阶段的交付与验收条件。

| 顺序 | 交付范围 | 发布验收 |
| --- | --- | --- |
| 1a 已接通 | 修复原版账户设置的改密操作。校验当前密码，按 Web 的 revokeOtherSessions 请求处理会话。 | A、B 两个浏览器登录同一账号。在 A 改密后，A 可继续操作，B 的旧会话与终端权限失效；旧密码登录失败，新密码成功。输错当前密码时两个会话均可继续使用。 |
| 1b 已接通 | 为桌面池实现空闲挂起。默认阈值参考 Rakazo 的十分钟，复用工作区归档；按整台 Team 电脑判断活动，区分挂起与手动停止。 | 用短阈值验证空闲回收；有控制租约、执行中的任务或 Shell 后台作业时继续运行。保存失败可重试，唤醒后文件可读。 |
| 1c 已接通 | 接通停机 Files 的目录与文本预览。Supervisor 读取最近一次完整归档并校验作用域和路径。 | 停机后列出并预览文件，运行容器数保持不变。覆盖 Team / Private 同名文件、跨空间访问和符号链接。 |
| 2 已接通 | 打通原版聊天附件。核对 artifacts 与 threads.send 契约，贯通上传、仅附件发送、按 Run 授权读取及结果文件返回。 | 从原版聊天上传文件并发起任务；核对工具实际读到的内容、刷新后附件可用、跨用户引用被拒绝。 |
| 3 | 扩展可见浏览器工具，从固定财经流程推进到授权网站的导航、观察、点击；配合原版 Activity 展示真实工具操作。 | 在两个授权站点完成同一项检索任务，用户能看到过程和来源；人工接管后 Agent 停止输入，失败时展示真实错误。 |

后续按原版产品域分批对齐。先做持久记忆及上下文压缩，再做独立 Task、Routine 和看板。邮箱注册已支持显式开关与独立个人空间；邮件验证、找回密码、删除账号与 OIDC 后续按原版部署配置分别验收。每项先确认原版页面入口、协议和权限语义，再排入开发。

Ubuntu 交付验证与上述功能并行，在目标架构上用实际域名和外部 PostgreSQL 验证 Compose、双账号隔离及数据库与工作区归档的配套恢复。

## 历史实施记录

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

Docker 交付采用外部 PostgreSQL、独立 API / Worker 和持有 Docker socket 的 Supervisor。原版 Web 可通过 bots.setComputer 切换模式，范围与验证步骤见 [交付指南](../infra/delivery/README.md)。

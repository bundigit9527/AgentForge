# AgentForge 开发协作规范

本文件供在本仓库工作的 AI 编程助手使用，适用于整个仓库。开始修改前阅读相关代码与文档；用户在当前任务中的明确要求优先于本文件。已有授权在其范围内继续有效，无需重复确认。

## 项目定位与实现范围

- AgentForge（智构）是采用 MIT 协议的企业级 Agent 平台，后端统一使用 Python。用户已确认优先完善 Python Harness，暂不为执行引擎引入 Node.js / Pi；前端既有工具链不受此约束影响。
- 当前已实现 API、Worker、模拟 Harness、数据库任务队列、任务与事件持久化、提交去重、SSE 续传和租约续期。已有版本化 Agent、会话、消息、执行尝试和不可变运行快照；原版 Rakazo Web 支持邮箱密码登录、设置保存、聊天和清空；同一会话串行提交，历史上下文固定最近 20 个成功轮次；新真实模型快照支持字符预算和完整轮次裁剪，旧快照保持 reject 策略。裁剪不是语义摘要或长期记忆，详见 docs/context-management.md。
- 已实现可配置的 Chat Completions 模型适配器、多轮工具循环和用量事件，默认保持模拟模式；DeepSeek 官方接口已在临时 SQLite 库通过两轮聊天与工具回传验收，企业路由与生产验证仍待完成。已有服务端模型目录、用户默认模型与 Bot 模型选择，DeepSeek 协议模式发送 max_tokens 并禁用思考模式；已支持可选 SSE 文本流、受限批量持久化、原版 thread.progress 与刷新恢复；不要宣称已支持思考字段、OAuth 或强制中断上游推理。企业单点登录、长期记忆、通用 Computer 工具沙箱、独立 Web 和桌面端仍属后续建设内容。目录中的职责说明不代表功能已经实现。
- 已实现纯函数工具注册、Agent 工具子集、部署级允许列表、调用记录与结果验证；远程 MCP 已通过官方 Python SDK 接入 Streamable HTTP，只允许部署配置审定的只读、幂等工具，并核对服务端提示；已有本地身份、admin / member / viewer 空间角色和显式空间共享；企业单点登录仍待接入；审批暂停恢复与外部操作台账见本文末尾。
- Web 与桌面端保留 React / TypeScript、Electron 的技术方向；不要把“后端使用 Python”扩大为重写所有客户端。
- 当前离线测试使用 SQLite；生产数据库目标是 PostgreSQL。明确区分离线验证、真实数据库验证和生产验证。

- Rakazo 源码保持原版，当前核查基线为 `d165de43`。协议、登录、数据映射和行为兼容应在 AgentForge 后端实现；不要再向 Rakazo 添加专用组件、契约、事件流逻辑或登录分支。浏览器测试与接入文档放在本仓库，运行时通过原版 `API_PROXY_TARGET` 配置指向 Python 后端。未实现接口返回错误，保留原版页面自身的入口和错误处理。
- 架构以 Rakazo 的模块职责和执行链路为基准，差异与未完成能力见 `docs/rakazo-alignment.md`；不要把对齐目标描述为已经全面实现。
- Skill 已支持四个内置配方、HTTP JSON Hub 列表与安装、作用域内版本副本、Agent / Run 快照和 `skill_read`。技能正文是操作说明，不赋予工具权限。Hub 在 API 的连接器中访问，Worker 只读取本次运行快照。原版 agentSkills.list/get/create/update/remove 已接通；聊天 /技能名 只为单次 Run 选择并固定技能，requested_skill 不改变 Agent 配置或工具授权。目录解析须位于创建事务内、提交去重之后，不能让重试追随新版本；个人技能编辑生成不可变版本，原版 id 是资产 ID、REST reference 是版本 ID；删除不擦除历史副本、不代替任务取消。个人技能保持所有者私有，录制示教仍未实现。

## 先阅读的文档

- [README.md](README.md)：项目概览与当前实现范围。
- [docs/architecture.md](docs/architecture.md)：架构基线与模块边界。
- [docs/development.md](docs/development.md)：环境配置、检查命令与手动调试。
- [docs/task-execution.md](docs/task-execution.md)：幂等、事件流、取消与租约协议。
- [docs/model-routing.md](docs/model-routing.md)：模型配置、接口边界、工具循环和错误码。
- [Agent 与会话](docs/conversations.md)：Agent 版本、消息、执行配置快照、尝试记录及手动调试。
- [版本化工具](docs/tools.md)：工具注册、允许列表、输入输出验证、执行记录和调试。
- [Skill 与广场安装](docs/skills.md)：内置、安装、版本快照和按需读取。
- [本地身份与权限](docs/identity.md)：身份初始化、令牌、成员角色、共享和执行撤权。
- [Rakazo 架构对齐](docs/rakazo-alignment.md)：实现映射与后续边界。
- [Rakazo oRPC 接入](docs/rakazo-orpc.md)：兼容契约、浏览器登录、持久化会话事件与验证方法。
- [docs/roadmap.md](docs/roadmap.md)：后续阶段与验收目标。

发现文档与代码不一致时，先核实实际行为，再随本次变更更新相关说明。不要将规划功能描述为已完成。

## 模块边界

| 路径 | 职责 |
| --- | --- |
| `apps/api/agentforge_api/` | FastAPI 应用工厂、身份解析、请求校验、任务接口与事件流 |
| `apps/worker/agentforge_worker/` | 任务领取、执行协调、租约续期、取消与恢复 |
| `packages/core/agentforge_core/` | 身份范围、领域对象、状态与环境配置 |
| `packages/contracts/agentforge_contracts/` | HTTP 请求与响应结构 |
| `packages/storage/agentforge_storage/` | SQLAlchemy 表定义、数据库连接与事务化数据访问 |
| `packages/runtime/agentforge_runtime/` | 模型接口、Harness 与受控工具调用 |
| `packages/skills/agentforge_skills/` | 内置 Skill、安装目录与选择服务 |
| `packages/connectors/agentforge_connectors/` | 企业外部接口适配，当前为 Skill Hub 和受控 MCP 远程工具 |
| `migrations/` | Alembic 数据库迁移 |
| `tests/` | API、隔离、幂等、事件流、Worker 与迁移的行为验证 |
| `infra/` | 开发部署配置与未来的工具沙箱配置 |

- 当前采用单一 Python 发行包，源目录与导入名由根目录 `pyproject.toml` 显式映射。新增包时同步维护映射，不用临时修改 `sys.path` 解决正式代码的导入问题。
- Rakazo Web 接入采用 Python `/rpc` 兼容层，保留 REST；两者复用存储事务和 Worker，禁止增加 HTTP 自调用或第二套执行循环。兼容范围固定在已测试的 Rakazo 契约；未实现的写操作返回明确错误，不能伪造成功。浏览器 Cookie 仅用于配置好来源的 RPC / 登录入口，REST 保持 Bearer 认证。原版邮箱密码登录由管理员显式配置，数据库仅保存加盐 scrypt 摘要；邮箱注册由 AGENTFORGE_REGISTRATION_ENABLED 显式开放，新账号创建独立个人空间，不授予部署管理权限；密码找回、企业 SSO 尚未实现，不伪造成功。密码重设须使现有密码会话失效，不撤销无关 API 令牌。
- API 不承担耗时的 Agent 执行；Harness 在 Worker 中运行；数据库访问与事务规则集中在存储层。
- 未来的脚本、浏览器与文件工具进入独立 Computer 环境；Docker 管理职责集中在 Supervisor。不要给 API、Worker 或 Computer 挂载 Docker socket。
- 优先沿用已有模块和接口；不要因目录拆分就引入独立微服务或新的队列组件。

## 环境与常用检查

使用 Python 3.12 和 uv。依赖声明在 `pyproject.toml`，版本锁定在 `uv.lock`；修改依赖时同步更新锁文件。以下命令均在仓库根目录执行。

首次安装依赖（需要依赖源可访问）：

```bash
uv sync --locked
```

依赖安装后执行离线检查：

```bash
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
uv run --locked --offline pytest
```

开发中可以先运行相关测试，例如：

```bash
uv run --locked --offline pytest tests/test_worker.py
```

- Ruff 配置以 `pyproject.toml` 为准：Python 3.12、100 字符行宽及已有检查规则。遵循现有类型标注、命名和导入风格。
- 默认测试使用临时 SQLite 数据库、模拟模型和进程内 HTTP 客户端，不依赖运行中的 API、Worker、Docker 或模型服务。
- 显式 PostgreSQL 集成验证使用 `--database-backend=postgresql` 与 `AGENTFORGE_TEST_DATABASE_URL`，每个用例使用独立临时 schema。不得复用应用数据库配置，操作步骤见 [docs/postgresql-testing.md](docs/postgresql-testing.md)。默认测试仍为 SQLite。
- 代码改动应运行相关测试及 Ruff 检查；涉及任务状态、权限、事务、协议或依赖的改动，完成全套离线测试。同一版本已通过的检查不必反复执行。
- 仅修改文档时，核对路径、命令、内容一致性和空白格式即可，不必启动服务或重复完整测试。
- 验证受环境限制时说明未完成的检查及原因，不将未执行的检查报告为通过。

## 开发运行与数据库

- macOS 开发使用 Colima 的 Docker 运行时承载 PostgreSQL，API 与 Worker 在本机 Python 环境调试；容器命令显式使用 `docker --context colima compose`，不自动切换全局 Docker 上下文。
- 当前 Compose 文件为 `infra/compose/compose.dev.yaml`，仅包含开发 PostgreSQL，默认映射到 `127.0.0.1:55432`。Ubuntu 生产方向为 Docker Engine、容器化后台与独立工具沙箱，可使用外部 PostgreSQL。
- 配置模板为 `.env.example`，本地配置为被 Git 忽略的 `.env`。应用不自动加载 `.env`；手动联调通过 `uv run --env-file .env ...` 显式加载，Compose 使用 `--env-file .env`。
- 除非用户已要求或授权相关联调，不启动 Colima、容器、API 或常驻 Worker，不连接真实模型服务或对真实数据库执行迁移。离线测试不需要这些操作。
- 数据库结构由 Alembic 管理，应用启动不得自动建表。结构变更新增迁移，保留已有数据，并同步模型、就绪检查及迁移测试；不随意改写已应用的迁移。
- 清空数据库、删除数据卷、覆盖环境配置等操作需要明确授权；普通代码修改不包含这些操作。

## 必须保持的行为

- **身份与隔离**：身份范围由服务端解析，任务与事件访问始终校验租户、空间和用户。客户端或模型提供的身份字段不能授予权限。开发令牌不能当作企业认证；缺少配置时不得放行任务接口。`database` 模式依据数据库成员关系授权，令牌只保存摘要；身份和令牌由显式管理命令初始化，或在部署启用注册后通过原版表单创建独立个人空间；启动不自动创建账号。资源默认私有，空间共享只授予读取或使用，写入仍需所有者权限；管理员不自动读取他人私有资源。Worker 在模型、工具、结果写回检查点复查成员与借用资源权限，运行快照不得固化角色或放宽当前策略。
- **事务一致性**：任务状态与对应事件保持事务一致，避免已改变状态却丢失事件。首版任务表兼任持久化队列。
- **配置版本**：REST 会话使用创建时固定的 Agent 版本；为保持原版 Bot 设置语义，RPC 发送在事务中读取最新 Agent 版本，但每个 Run 一旦创建，其版本、配置和重试均不可变。不要为更新设置重写历史 Run 或静默清空消息。
- **提交幂等**：同一身份范围内，相同 `Idempotency-Key` 与请求内容复用同一任务；相同键但不同内容返回冲突。不同身份范围之间不能复用任务。
- **租约与取消**：只有持有有效租约和当前执行标识的 Worker 可以开始新工具调用或写回结果；取消、失效或重新领取后，旧执行不能覆盖任务结果。
- **副作用与恢复**：现有自动重试适用于无外部副作用的模拟工具。模型请求在崩溃恢复时可能重发并重复计费。接入真实工具时，为外部操作设计独立幂等与结果核对；未知结果不能直接重放。取消状态不代表正在运行的同步函数已被强制终止。
- **上下文预算**：策略和字符预算固定在模型快照中；缺少新字段的旧快照保持原有计数及超限失败行为。裁剪只发生在本次执行的历史副本，按最早完整 user / assistant 轮次移除；不得裁剪指令、当前问题、Skill 目录、工具定义或工具调用参数。仅新 compact_tool_results 快照可按 docs/context-management.md 缩短请求副本中的工具正文，保留调用配对、控制字段和原始审计；旧快照结果保持完整。context_trimmed 只记录计数，仍须校验租约和当前权限；不改写消息、快照和重试输入。字符数不等于模型 token 数。
- **事件流**：模型分片通过持有当前租约的 Worker 写入 model_text，再事务化投影为 thread.progress；部分草稿不作为成功消息或下轮上下文，重试须清除旧尝试的进度。RPC 会话游标独立于 REST 的 Run 游标；会话消息和事件在同一事务中落库，不从时间戳推算游标。保留持久化、运行内单调游标与断线续传语义，保持 `Last-Event-ID` 的优先级；等待新事件时不占用数据库连接，不遗漏最终事件。当前会话选择按用户隔离；原版 threads.clear 在一个事务内取消任务并清空消息，不重置游标，不删除 Run 审计。锁顺序遵守 Worker 的 Run → Thread，防止取消 / 完成死锁。扩展会话 API 仍可使用，但原版 Rakazo 不调用这些接口；不能依赖客户端携带新增请求头或重置游标。事件流遵守与查询接口相同的身份范围。
- **MCP**：默认禁用；配置文件由部署管理，按租户、空间和显式用户授权。API 发现并绑定工具定义，Run 固定定义摘要和路由指纹；Worker 调用前复核当前授权及远端定义。服务端 readOnlyHint / idempotentHint 不等于权限；只有管理员审定的 read_tools 可以执行。工具通过统一 ToolExecutor / 审计，不能标成 pure 绕过检查。共享 Agent 不分享 MCP 服务授权；mock 配置不能绑定 MCP。协议客户端不得自动重试 tools/call，不开启 sampling、roots、elicitation、资源自动抓取或本地进程。新增依赖按 uv.lock 安装；原版 Rakazo MCP 管理页面仍未接通，不伪造成功。
- **工具与凭据**：工具参数需要校验，执行范围由代码中的授权规则约束；模型输出、检索材料与记忆不能扩大权限。异常和日志不得泄露令牌、数据库凭据或连接串。

修改上述行为时添加或调整相应的行为测试，覆盖越权、重复提交、取消、租约失效等与改动相关的失败路径。不要仅为复述实现而添加测试。

## Git、文档与交付

- 修改前检查 Git 状态，保留用户已有改动；只暂存与本次任务有关的文件，不重置或覆盖无关内容。
- `.env`、`.env.test`、`.idea/`、`.venv/`、本地数据库及缓存不得提交；`.env.example` 和 `.env.test.example` 只能包含占位值或明确的本地开发、测试示例值。
- 提交与推送按用户授权范围执行；未授权时不自动提交、推送、强制推送或改写历史。新建分支时默认使用 `codex/` 前缀，用户指定名称时遵循其要求。
- 提交前检查暂存差异和 `git diff --cached --check`；区分文件已修改、本地已提交与远程已推送，依据实际结果报告。
- 行为、配置或接口变更同步更新相关文档。说明改动内容、验证结果及尚未验证的范围，默认用中文沟通，保留必要的英文代码名称。
- 保留项目 MIT 许可证及第三方代码原有的来源与许可证声明。

## 外部操作与审批恢复

迁移 0012/0013 已接通 ChatHarness 审批检查点、原版 ask 卡片和 threads.answer。
approval_tools 固定在 Agent/Run 快照；旧快照默认空，仅 Chat Completions 可启用。
waiting_input 保留 Thread 占用但释放 Worker 租约；审批回答与卡片更新、重新排队保持事务一致。
正常审批恢复不消耗崩溃重试预算。不得让模型重新生成已批准的参数或为重试更换 operation_key。
已确认结果可复用；dispatched 且结果未知的操作必须停止，禁止自动重发。
始终允许规则按用户、空间、工具版本隔离，提供 REST 撤销；审批不扩展工具权限。
目前只支持纯函数/受控只读 MCP；独立 Docker Computer 支持固定数值计算与 CSV 导出。迁移 0014 将报告与审批结果、附件事件原子保存，下载按 Run 所属用户和空间授权。外部写连接器、自动审核及通用脚本执行仍待接入。
详情及手动验收见 docs/external-effects.md。原版 Rakazo 源码不变。

Ubuntu 的单机 Compose 配置见 infra/delivery。迁移 0015 保留旧 Bot 的 Private 模式，新建 Bot 默认 Team。团队桌面只在同租户、同空间内共享，屏幕票据和控制租约必须绑定实际 Bot 和用户会话。API 当前须单进程运行。

原版 runs.list 已接通当前用户所选 Bot 会话的 Activity，终态列表最多 20 条。清空会话后不展示原输入摘要，底层 Run 审计保留。工具 Activity 从同一 ToolJournal 事务投影；参数、结果和凭据不写入 UI 活动摘要。成功工具用 steps，失败／未确认用 meta；不能把 abandoned 状态投影为成功。详情见 docs/activity.md。

可见浏览器首版见 docs/visible-browser.md。后端保持纯 Python，不引入 Graphile Worker / Node.js。Browser 工具只能由有效 Run / Attempt 调用，授权来源固定在运行快照并逐步复核当前部署策略；公网模式仅允许公开 HTTPS HTML，Supervisor 固定 DNS 解析地址并过滤凭据。Computer 仍无网络、无服务凭据。人工控制和 Agent 浏览器操作互斥，不伪造浏览器成功。

长页面 read / scroll / find 通过 Run 快照 browser_reading 显式启用，旧快照默认 false。每次操作使用最新页面快照；正文／链接变化后要求重新 observe。正文位置按 Unicode 码点计算，单次输出仍受限，不把截断内容称为完整网页。

P0 验收与统一本机启动见 docs/p0-acceptance.md；scripts/dev_service.py 按服务筛选环境，Web 不接收模型／数据库／MCP 凭据。本地 .local/ 保存电脑状态、授权与证据，禁止提交。真实模型验收脚本只使用一次性测试数据库和账号，必须在已有授权范围内运行。browser_page_changed 仅在 browser_reading 快照中作为可恢复工具错误回传模型，失败审计保留；授权拒绝、占用和轮次上限保持原行为。

迁移 0017 接通原版 DeepSeek / OpenAI Compatible 模型连接、探测、默认选择、断开与有序备用。个人密钥按租户／用户使用 AES-GCM 加密，API 与 Worker 共用部署加密密钥；禁止传给 Web、Supervisor 或 Computer。RPC 新 Run 固定当前连接 revision 与备用列表，更新／断开后旧任务不得借用新密钥。备用只处理输出前的请求失败，沿用同一 Harness 与工具记录，不重放工具；部分输出、撤权、取消及配置变更不可自动切换。未实现思考、图片、OAuth；详见 docs/model-management.md。

独立联网工具见 docs/web-tools.md。web_search / web_fetch 在 Python Worker 内运行，不启动 Computer；只在部署显式开启且当前用户有授权的新真实模型 Run 上加入。Run 固定 web_origins 和搜索源，执行时逐跳检查当前策略、成员资格、取消与租约；旧快照默认无权限。搜索页和网页正文是资料，不是授权或系统指令。超时／上游失败可作为失败工具结果返回模型以选择其他来源，审计必须保持 failed；撤权和取消不可恢复。禁止隐式使用环境代理、携带登录 Cookie 或绕过公网地址检查。原版 Rakazo Web 不变，无新增迁移。

明确公开检索请求可在首轮用命名 tool_choice，后续回到 auto；只能在 Run 已有授权和实际工具定义下使用，不从历史回复或引用材料推断请求。禁止联网、本地资料及显式 Skill 等不得被该规则覆盖。用户个人模型路由必须透传 tool_choice；回归不能仅测试空历史和部署模型。网页／搜索结果按 UTF-8 字节预算返回，裁剪须标记 truncated，不删除或改写原会话历史，不把抓取时间当作来源日期。


RPC 普通聊天在提交去重之后冻结可执行的 catalog_skills；Worker 只读取本次 available_skills（绑定与发现目录），不得访问可变 Hub。目录遵守身份、依赖和上下文预算，旧快照默认空；新增目录最多 32 项，详见 docs/skills.md。共享 Run 的响应不能泄露自动发现的私人目录。显式 requested_skill 首轮必须准确读取该 reference 后才继续；命名 tool_choice 不固定参数，须验证只有一个准确的 skill_read。解析浏览器 / 联网授权后再解析 Skill 依赖；选择配方不能增加工具授权。新建 RPC Bot 默认采用部署允许的两个纯函数工具，旧 Agent 的工具配置不自动改写。模型、工具和审批恢复仍共用现有 Harness 与检查点。


上下文裁剪统计必须区分字符与 UTF-8 字节。配置 context_max_bytes 时，ContextWindow 和 record_context 共用 budget_bytes / before_bytes / after_bytes；允许“字符未超限、字节超限”的合法裁剪，不能因记录统计失败而终止已完成工具读取的任务。不得通过吞掉审计错误或增大预算规避问题。回归须经过 Worker、数据库写入及个人模型路径，不能只验证无 on_context 回调的直接 Harness。


Python 自动文件生成见 docs/python-execution.md。Run 固定 python_image，默认 None；只有显式用户授权的新真实模型 RPC Run 启用 python_execute，再解析 Skill 依赖。Worker 检查当前权限、镜像和租约；Supervisor 在离线临时容器中运行脚本，禁止在 API/Worker 宿主机 eval/exec 模型代码。GeneratedToolResult 的文件字节不进入模型上下文，complete_tool 同事务保存工具、附件与消息；取消与失效租约不得发布。只对 python_execute 允许这一产物通道，不能把任意工具标为 pure 绕过授权。PPT生成是第四个内置 Skill；原版 Rakazo Web 不变。旧工具每次调用为空工作目录，不能宣称已支持桌面持久工作区或通用 Shell。


迁移0018增加不可变附件版本关系。新授权 Run 使用 python_execute@2、python_continuity 和冻结的 python_files；旧默认 false/空，绑定旧PPT配方的配置保留v1工具。文件发现限当前用户、Bot、未清空会话的成功任务与显式附件；共享 Run 隐去私人文件清单。实际字节读取必须检查当前租约和权限，只可读取冻结版本或本Run产物。输入别名匹配导出名才建立新版本，原字节不覆盖；文件族按固定顺序加锁分配版本，工具/附件/版本/消息同事务。修改旧版创建有确切parent_id的新版本，不自动合并。容器仍为临时目录，选定输入通过受限协议传入，绝不挂宿主机工作区。详见 docs/python-execution.md。


电脑文件互通见 docs/workspace-bridge.md。workspace_enabled 默认false，须同时具备Python连续文件能力和显式workspace授权；Run固定实际电脑模式，workspace_save@1必须加入approval_tools。list_files / attach_file只访问当前Bot工作目录，后者可经受控GeneratedToolResult通道发布附件；不要将这项例外扩展到任意read工具。workspace_save和write_file是仅有的注册write适配器，API须复核活动工具及同参数dispatched台账，不能仅凭服务令牌放行。文件副本按artifact_id生成固定路径，已有内容不符时拒绝覆盖；Team共享风险在原版审批卡片中说明。Supervisor检查执行授权与代次，Agent与人工控制互斥。复制后取消/断线/快照失败可能有未知副作用，禁止自动重放或宣称磁盘已回滚。旧attach_file@1保持path语义；新workspace_file_tools快照支持read_file/write_file和attach_file@2命名版本，shared/、shell/open_path仍待对齐，不能宣传已完全复刻Rakazo。

2026-11-10：已知个人模型窗口来自审定目录或显式覆盖；旧连接缺少来源时不得擅自扩大。新Run固定compact_tool_results/context_window_tokens；工具投影只缩短正文，不改审批checkpoint、Skill正文、参数或原始工具记录。context_trimmed支持仅工具裁剪，依旧受当前租约与权限检查。


2026-11-11：workspace_file_tools默认false，开启须具备workspace_enabled及write_file@1审批。仅新快照启用文本读写和命名附件；工具预算不足5项但足3项保留旧互通工具。write_file新建明确expected_sha256=null，覆盖必须提交read_file返回的摘要；API核对活动版本、固定模式和同参数dispatched台账，Supervisor只执行固定UTF-8文件程序。确定的缺失/类型/摘要错误可交回模型且保持失败审计；写入未知结果不得重放。attach_file@2的name/description与调用记录核对，父版本只能来自冻结可用附件或本Run，清空和其他用户不得隐式匹配；标题与安全本地文件名分离。详见docs/workspace-bridge.md。

2026-11-12：新workspace_file_alignment快照默认false；只有新获授权RPC任务启用attach_file@3与write_file@2，旧@1/@2和旧审批规则保持原义。write_file@2新建时可在当前Bot目录内逐层创建父目录，拒绝符号链接，不给模型任意mkdir执行权限；确定文件拒绝不表示未留下空目录。attach_file@3标题去空白、无名使用basename，在同租户／空间／用户／Bot历史内忽略大小写匹配，清空聊天不删除文件版本。归族不授予旧文件字节读取权；Python输入和workspace_save仍受冻结清单／本Run限制。父版本须在complete_tool事务内由服务端解析，不相信模型parent_id。PostgreSQL按名称身份加事务锁，沿用文件族行锁；SQLite先取得写锁。迁移0019仅回填名称摘要索引，保留字节、版本与旧Run；新增HTML/GIF/WebP及别名只对新attach_file开放，Python导出格式不静默改变。原版Web不改动，open_path、渲染、通用Shell和Team shared/仍待开发。

# 使用原版 Rakazo Web

策略：Rakazo 源码保持原版，当前验证基线为 `d165de43`。此前对登录、页面、会话选择、版本提示、错误浮层、共享契约和订阅模块的定制均已收回。AgentForge 负责兼容原协议与行为；浏览器测试和本文档均保存在 AgentForge 仓库，不向 Rakazo 添加文件。

原版 Computer 面板可查看和接管已分配的桌面，聊天与 Files 页面支持下载本次运行生成的 CSV。语音、Hub 管理页面、示教录制、模型凭据管理和 SSO 等入口仍保留，未接通的操作由后端返回错误。

当前统一启动入口和 2026-11-06 的真实 PostgreSQL / DeepSeek / Computer 验收见 [P0 记录](p0-acceptance.md)。

## 本地启动

AgentForge 配置继续使用原有 `.env`，例如：

```dotenv
AGENTFORGE_AUTH_MODE=database
AGENTFORGE_WEB_ORIGIN=http://127.0.0.1:5173
```

在 AgentForge 目录升级数据库（当前最新 `0016`），并为已存在的成员设置 Web 登录：

```bash
uv run --env-file .env --locked alembic upgrade head
uv run --env-file .env --locked agentforge-admin set-password \
  --tenant example-tenant --space example-space --user alice --email alice@example.com
```

命令交互式输入密码两次，至少 12 个字符；请替换租户、空间、用户和邮箱。此命令不会创建成员。已有环境不要重复 bootstrap；首次环境按 [本地身份](identity.md) 初始化。原有 API 令牌继续用于 REST，不能当作 Web 密码。

分别运行 AgentForge API 和 Worker，命令与 PyCharm 配置见 [开发文档](development.md)。在 Rakazo 目录使用其原有代理配置：

```bash
NODE_ENV=development API_PROXY_TARGET=http://127.0.0.1:8000 WEB_PORT=5173 pnpm --filter @rakazo/web dev
```

打开 `http://127.0.0.1:5173/sign-in`，使用刚设置的邮箱和密码。空账号按原版引导创建 Chief，再进入聊天。所有 `/api` 与 `/rpc` 交给 Python 服务；不为此启动 Rakazo 自身 API / Worker。

## 页面行为

- 知识设置的 Skills 区域可查看内置和已安装技能；聊天框输入 `/` 选择本次技能。工具授权和版本保持规则见 [Skill 接入](skills.md#原版-rakazo-web-接入)。个人技能可在原版页面新建、编辑和删除；示教录制仍未实现。
- 保存助手描述 / 指令后，下一次 RPC 运行采用最新 Agent 配置，旧 Run 的快照不改变。
- 原版清空按钮同时取消当前任务并清空消息，随后可继续发送；运行审计保留。
- 不增加会话下拉框、版本提示或 AgentForge 专用登录页。后端扩展会话接口留给专门接入的客户端；外部切换后原版页面应重新加载。
- 登录态到期或撤销遵守原版 Better Auth 刷新机制；没有额外的 AgentForge 定时检查器。
- 已登录用户可在原版账户设置中在线改密并使其他浏览器会话失效。可在 `.env` 设置 `AGENTFORGE_REGISTRATION_ENABLED=true`，重启 API 后使用原版注册页面，账号注册后自动登录并进入首次引导。新用户有独立个人空间；找回密码、删除账户和企业登录尚待接入。管理人员通过 set-password 重设本地密码，原版页面操作收到真实错误。

## 黑盒浏览器验证

测试仅使用临时 SQLite、模拟模型和测试身份，不读取 `.env`。测试路由只存在于显式 fixture，不进入正式 API。

AgentForge 目录运行：

```bash
uv run --locked --offline python tests/interop/browser_server.py --port 18000 --web-port 15173
```

Rakazo 目录运行原版 Web：

```bash
NODE_ENV=development API_PROXY_TARGET=http://127.0.0.1:18000 WEB_PORT=15173 RAKAZO_DEV_HOST=127.0.0.1 pnpm --filter @rakazo/web dev
```

另一个终端，在 AgentForge 目录指定已安装依赖的 Rakazo 路径：

```bash
export AGENTFORGE_RAKAZO_ROOT=/path/to/rakazo
node --import "$AGENTFORGE_RAKAZO_ROOT/node_modules/tsx/dist/loader.mjs" tests/interop/rakazo_web.mts
```

默认使用已安装的 Google Chrome，PLAYWRIGHT_CHANNEL 可选其他已安装渠道。脚本覆盖原版邮箱登录、错误凭据、首次引导、两轮聊天、设置生效、清空与取消、刷新、不同用户隔离、撤销及未实现设置报错。截图默认保存到临时目录，可用 AGENTFORGE_WEB_TEST_OUTPUT 指定目录；不在 Rakazo 中生成源码或测试文件。结束后以 Ctrl+C 停止两个临时服务。没有运行 Electron、Docker 或真实模型。

## 完整聊天手动验收

1. 在 `.env` 配置模型，DeepSeek 官方示例见 [模型路由](model-routing.md)；API 与 Worker 使用相同配置。密钥只放本机 `AGENTFORGE_MODEL_API_KEY`。
2. 按上文升级到 `0016`，以数据库身份模式启动 API、Worker 和原版 Web，使用管理员配置的邮箱密码登录。
3. 原版模型设置可选 `deepseek-v4-pro`；Bot 设置的模型下拉框也应包含该项。旧模拟 Bot 必须改选真实模型（或保存默认模型），否则仍按旧配置运行。
4. 第一轮发送：`请记住本次代号为星河。调用 example_report，把“今天完成模型接入”整理为报告草稿，再告诉我工具返回的内容。` 应看到工具执行事件与最终回复。
5. 第二轮发送：`本次代号是什么？上一轮工具整理了什么？` 应能引用第一轮上下文；刷新页面后，两轮消息仍存在。
6. 在 Bot 设置选择模型后提交新消息，核对新 Run 的 `execution_config.model`；旧 Run 模型和工具记录应不变。停止、失败和清空按原版入口测试，失败时展示明确错误。

后端可通过 `AGENTFORGE_MODEL_STREAM=true` 启用流式正文；重启 API / Worker 后为现有 Bot 重新保存模型配置，再到原版设置 **General → Advanced → Stream replies** 打开显示开关。该开关默认关闭；关闭时仍在该轮完成后显示答案。SSE 同时传递运行和工具状态。上述用例覆盖聊天与报告草稿。桌面接管、可见网页检索及审批后导出 CSV 需按 [Supervisor 配置](../apps/supervisor/README.md) 单独验收。模型连接配置不是连通性证明，真实调用成功后才能记录真实验收通过。

## 原版核对

在 Rakazo 目录执行 `git diff d165de43 --exit-code`，确认受版本管理的内容与原版一致。相对本地定制提交仍可能显示恢复改动，这不代表源码仍有定制。原有 `.idea/`、环境配置和其他未跟踪个人文件不属于源码恢复范围，不提交它们。

以下为历史验证记录；当前结果以 [P0 验收](p0-acceptance.md) 为准，目标 Ubuntu 生产部署仍待验证。

验证记录（2026-11-02）：后端 323 项通过、5 项 PostgreSQL 专用测试跳过；原版 Web 682 项单元测试与类型检查通过；黑盒 Chrome 完整流程通过，已检查原版登录和聊天截图。Ruff 检查通过。Rakazo 受版本管理文件与 `d165de43` 比较无差异，临时测试服务已停止。

本轮补充验证（2026-11-02）：后端 388 项通过、5 项 PostgreSQL 专用测试跳过；Ruff 和格式检查通过。包含实际原版 Better Auth / oRPC / Zod 客户端的登录、模型选择、多轮聊天、工具回传及失败返回测试。另以临时测试账号、临时 SQLite 数据库和真实 DeepSeek V4 Pro 完成相同核心流程：第一轮请求工具并回传结果，第二轮准确引用前文代号，共 3 次模型请求。真实验证使用进程内 API 与 Worker，未启动开发库、Colima 或常驻服务；本轮没有重跑 Chrome 页面测试，也未应用开发库迁移。Rakazo 与原版基线 d165de43 无源码差异。

开发环境接通记录（2026-11-02）：本地 PostgreSQL 已由 0009 增量升级至 0011，原有账号、两个 Bot 与历史消息保留。现有模拟 Bot 已通过 bots.update 创建新的真实模型版本，账号默认模型为 deepseek-v4-pro；API 与 Worker 使用本地 .env，Rakazo Web 通过原版代理连接。Chrome 页面验证通过：邮箱登录、Bot 模型选择展示、真实两轮对话、工具回传、SSE 结果展示与刷新后历史保留；页面无脚本错误。验证后开发服务保持运行供手工测试，生产部署和完整 PostgreSQL 集成测试仍需另行验证。

流式开发记录（2026-11-02）：原版前端源码未变，新增后端 model_text → thread.progress 映射、运行中刷新恢复与失败 / 取消清理。原版客户端 reducer 的显示与续传已离线验证，真实上游 SSE 已在独立临时环境验证。本轮没有重跑启用流式后的 Chrome 整页测试，也没有热切换现有开发服务：停止现有 API 的操作未获批准，因此当前页面继续使用之前已验证的非流式版本。

## 聊天附件验收

先显式升级数据库到 0016，启动同一版本的 API 和 Worker，选择 Chat Completions 模型。在原版 Bot 聊天框添加 CSV，输入“读取附件，计算合计并返回 result.txt”，核对返回文件的内容并刷新页面。也可只发附件。每轮最多四个文件，每个文件 1 字节至 10 MiB。

- 原版 Web 先调用 `artifacts.create`，再由 `threads.send` 按顺序绑定已上传的文件 ID。发送重试复用原 Run，改变附件列表须使用新消息 nonce；上传可显式指定独立 nonce。
- 输入文件保存在 PostgreSQL。模型看到文件清单，可通过 `read_attachment` 分页读取本轮附件。UTF-8 文本、Markdown、CSV、HTML 和 JSON 按文本回传，二进制文件与无法解码的片段按 base64 回传。PDF 提取、OCR、图像理解与 ZIP 解包需要独立解析工具。Files 面板继续管理桌面的 `/workspace`。
- `write_attachment` 可将最多 3000 个字符的文本、Markdown、CSV 或 JSON 发布为本轮的可下载文件。附件卡片、工具完成和结果文件在同一事务内写入。聊天文件按租户、空间、用户和 Bot 限定访问范围；当前每次独立上传产生一个不可变文件，附带的文件引用固定在本轮。群组聊天及同名文件版本管理仍待对齐。

## Activity 手动验收

打开原版侧栏铃铛 Activity，发送任务后可见排队／执行／等待审批；结束后进入最近记录。先在原版账户设置打开 Show tool activity，再展开回复中的工具记录，刷新后核对调用名称、次数及执行耗时。失败与被取消的未完成工具显示错误或结果未确认，不显示成功勾选。原版 Activity 最近记录为 20 条，搜索和日期过滤由原版页面处理。详细边界见 [执行记录](activity.md)。

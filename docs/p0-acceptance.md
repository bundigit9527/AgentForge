# P0：当前版本收口与本机验收

验收日期：2026-11-06。Rakazo Web 基线为 `d165de43`，AgentForge 使用 Python API、Worker、Harness 和 Supervisor。没有修改 Rakazo Web 源码，也没有引入 Node.js 后端；Node.js 只用于原版 Web 和浏览器测试工具。

## 2026-11-20：原版 Web 视觉组合验收

已在 macOS / Colima、一次性 PostgreSQL 17、真实北京地域 `qwen3-vl-plus`、Python API/Worker/Supervisor 和原版 Rakazo Web 上完成。共 14 项页面检查、7 个真实任务，无未捕获的页面异常。P0 的本机组合验收已收口；目标 Ubuntu 交付验收仍独立安排。

| 检查范围 | 本轮结果 |
| --- | --- |
| 视觉与操作 | Alice Team、保留历史切换到 Private、Bob Private 三轮均通过原版上传/发送/审批，执行 launch_app → workspace_save → open_path → computer_observe → computer_act；模型正确指出红色面板右侧裁切与翻页后的绿色面板。 |
| 审批刷新 | 等待审批时刷新，effect_id 和参数保持一致；最终回复刷新后仍可见；同一工具调用没有重复执行。 |
| 双用户访问 | 另一用户无法读取私有 Run、工具记录、审批和附件，也无法为对方 Bot 申请屏幕链接；使用真实审批卡片 ID 代批被拒绝。Team 电脑仍按设计共享画面，不能将其称为屏幕私有。 |
| 控制权 | Team 接管互斥、两个 Private 可独立接管。电脑动作暂停审批时人工接管，批准后 Agent 动作被阻止；交还不自动重放失败操作，新任务重新观察并审批后可执行。 |
| 实际鼠标键盘 | 原版 Take control 后等待 noVNC 控制连接及真实画面；鼠标点击、Home 按键后，画面从绿色区域回到红色区域。检查真实画布像素并保存截图，不只检查 controlHolder。 |
| 拒绝与取消 | 原版 Deny 和 Stop 均阻止 computer_act；取消保持 cancelled，拒绝后的模型说明可正常完成，不把被拒绝的动作记为成功。 |

本轮 7 个任务包括 5 个 succeeded、1 个预期的接管阻止 failed、1 个预期的 cancelled。证据保存在 `/tmp/af-web-vision-20261120-c`：`result.json`、`backend-result.json`、各轮聊天/桌面/模型截图和 `manual-control.png`。结果 JSON 不包含模型密钥、登录密码或签名屏幕链接。原版“Teach a task”仍会显示未实现错误，未隐藏入口；这不属于本轮视觉动作验收。

验收代码位于 `tests/interop/rakazo_vision.mts`，由现有一次性服务脚本的 `--vision` 模式运行。首轮在测试脚本打开面板时出现页面加载竞争，已按原版按钮的实际展开状态等待；随后加强 noVNC 重连和按键生效断言，最终整轮通过。没有修改 Rakazo 或生产执行逻辑。

复现前准备已安装依赖的原版 Rakazo、Chrome、运行中的 Colima、带预览依赖的桌面镜像，以及本机忽略文件 `.env` 和 `.env.vision`。`.env` 的 `AGENTFORGE_PYTHON_IMAGE` 需为已有不可变镜像摘要；视觉文件使用 `AGENTFORGE_VISION_BASE_URL`、`AGENTFORGE_VISION_MODEL`、`AGENTFORGE_VISION_API_KEY`。镜像构建见 [工作区预览](workspace-bridge.md)。API 18000、Web 15173、计算服务 18766 需空闲，均可通过脚本参数调整。

```bash
uv run --env-file .env --env-file .env.vision --locked --offline python tests/interop/p0_acceptance.py \
  --rakazo-root /Users/jacky/Documents/Ai/rakazo \
  --vision --image agentforge/desktop-preview:acceptance \
  --output .local/acceptance/vision-web
```

该模式仅在测试进程中切换 Qwen 配置，工具轮次上限为 24；创建两个临时用户，不使用开发库。退出时清理临时数据库、桌面和子进程，不覆盖 `.env` 或 `.env.vision`。脚本要求 Docker 已运行，不负责启动或停止 Colima；本轮验收完成后另行停止了 Colima。以下保留首次验收记录和常规开发启动说明。

## 2026-11-06 基线完成项

- 接通 Activity 运行列表、工具步骤、失败状态与刷新恢复。
- 接通可见 Chromium 的打开、观察、链接点击、长页面读取、查找和滚动。
- 修复真实模型联调发现的过期快照问题：当前阅读能力启用的 Run 收到 `browser_page_changed` 后，将失败结果返回模型，允许通过新的 `browser_observe` 调用恢复。原失败工具记录保留，不把失败动作标为成功；授权拒绝、电脑占用、取消和轮次上限仍会阻止继续执行。
- 增加 `scripts/dev_service.py`：从同一份本机配置启动各服务，Web 不继承模型、数据库和 MCP 凭据，Supervisor 仅接收其需要的配置。
- 补齐本机桌面池、镜像、工作区归档、浏览器授权与专用令牌；`.env`、`.local/` 和测试证据不纳入 Git。原配置有本地备份，开发数据库未迁移、未清空。

## 验收结果

| 检查 | 结果 |
| --- | --- |
| SQLite 完整回归 | 638 通过，5 项 PostgreSQL 专用检查跳过 |
| PostgreSQL 17 完整回归 | 643 通过 |
| 原版 Web + 真实 DeepSeek V4 Pro | 登录与首次引导、模型选择、工具调用、多轮上下文通过 |
| 实际 Computer 浏览器 | 打开 Python 官网、查找、分页读取链接、点击 About；画面通过原版 noVNC 展示 |
| 聊天附件与产物 | 原版上传输入附件、工具读取、生成结果文件；下载内容包含实际来源 `https://www.python.org/about/` |
| Activity | 完成状态、真实工具名称、页面刷新后记录保留 |
| 控制与隔离 | 原版接管／交还、同空间 Team 控制互斥、Private 独立控制、另一账号无法读取私有聊天和 Run |
| 取消 | 原版 Stop 将任务置为 cancelled，失效执行不能写回成功结果 |

真实整链路验收使用独立临时 PostgreSQL、随机测试密码和一次性桌面。真实模型密钥读取操作员本机环境；不会复制密钥到 Web 或 Computer。测试有真实模型调用费用。测试使用 16 轮工具循环上限、120 秒模型请求超时和上游流式输出；这些测试参数不会修改现有 Agent 或历史 Run 的快照。

运行结束自动停止测试服务、销毁临时测试数据库和桌面；开发 PostgreSQL 数据卷保留。目标 Ubuntu、实际生产域名、外部 PostgreSQL、生产容量与备份恢复仍需独立验收。本次不是生产部署认证。

## 本机手工启动

先进入 AgentForge 根目录。当前本机配置已写入忽略的 `.env`，浏览器授权保存在 `.local/browser.json`，只覆盖明确列出的现有身份，新注册账号不会自动获得浏览器权限。授权的公开 HTTPS 模式仍不支持登录、JavaScript、表单和业务提交。

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py check
colima start --runtime docker
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml up -d --wait
```

分别在四个终端中执行以下命令；每个终端均从 AgentForge 根目录启动。Supervisor 应先于首次电脑操作就绪。

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py supervisor
```

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py api
```

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py worker
```

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py web \
  --rakazo-root /Users/jacky/Documents/Ai/rakazo
```

Web 为 `http://127.0.0.1:5173`，API 为 `http://127.0.0.1:8000`。在其他机器替换 Rakazo 路径，并按 `.env.example` 与 [浏览器配置](visible-browser.md) 准备本机环境。启动器不会迁移数据库、创建账号或重设密码。

手工验证：上传一个文本附件，发送“读取附件，并用电脑浏览器打开 https://www.python.org/，查找 Python，然后点击 About，返回实际来源并生成 result.txt”。查看电脑时先点右侧预览中的 Open，再在完整窗口接管或交还。原版账户设置的 Show tool activity 可显示工具记录。

`write_attachment` 当前随本轮输入附件启用；只有文字、没有输入附件的普通聊天不能据此假定已有文件生成工具。当前新建 Bot 默认工具为 `example_report`，其他业务工具需要显式绑定。

停止时先在 Web 停止活动任务、交还电脑，再在各终端 Ctrl+C；等待 Supervisor 保存工作区后停止开发库和 Colima：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml stop
colima stop
```

不要使用 `down -v`，它会删除开发数据卷。

## 重跑真实验收

需要已经安装依赖的原版 Rakazo、Google Chrome、运行中的 Docker，以及本机 Computer 镜像。API 18000、Web 15173 须空闲。脚本不使用开发数据库、不注入测试专用 API。

```bash
uv run --env-file .env --locked --offline python tests/interop/p0_acceptance.py \
  --rakazo-root /Users/jacky/Documents/Ai/rakazo \
  --output .local/p0-evidence
```

结果保存在 `result.json`、`backend-result.json`、`tool-summary.json`、结果文本与页面截图中。退出码非零表示本次未通过，不能仅凭服务已启动判断验收成功。证据可能包含测试会话和本机地址，保留在忽略目录内。

自动化中的浏览器工具只访问 Python 官网；长页面 Unicode、截断及第 61 个链接的专门验收仍见 `tests/interop/browser_reading_smoke.py`。

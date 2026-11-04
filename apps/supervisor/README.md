# Docker Computer 计算服务

Supervisor 是独立运行的 Python 服务。首个工具 `summarize_numbers` 接受 1–512 个整数，单值范围为 ±10¹²，返回数量、最小值、最大值、总和与均值。每次调用在新容器中运行固定 Python 程序。用户数据从标准输入传入，程序、镜像与 Docker 参数由部署者控制。

Worker 通过现有 Streamable HTTP MCP 连接器发现和调用工具。租户、空间、用户授权、运行租约及工具定义核对由 AgentForge 执行，Supervisor 单独验证服务令牌。该令牌可直接调用计算服务，应仅分发给受信的服务进程。容器按调用销毁。CSV 报告由 AgentForge 按运行归属保存，下载时校验租户、空间和用户。

## 本机调试

1. 在 Colima 中准备可信的 Linux 镜像。镜像应有可供非 root 用户执行的 python3，且不含业务凭据。读取本机现有镜像 ID，例如：

   ```bash
   docker --context colima image inspect --format '{{.Id}}' rakazo/computer:local
   ```

2. 在 AgentForge 根目录创建仅本机可读的 `.env.supervisor`。设置 `DOCKER_CONTEXT=colima`、`AGENTFORGE_COMPUTER_IMAGE`（上一步完整的 sha256 ID）和 `AGENTFORGE_MCP_COMPUTER_TOKEN`（推荐 32 字节随机数的十六进制表示，可通过本地密码管理器生成）。Supervisor 仅需这三个部署值。Ubuntu 用 Docker Engine 的本机上下文。

3. 在根目录运行单进程 Supervisor：

   ```bash
   uv run --locked --env-file .env.supervisor uvicorn agentforge_supervisor.app:create_app --factory --host 127.0.0.1 --port 8766 --workers 1 --limit-concurrency 16
   ```

4. 将 `infra/computer/servers.example.json` 中的服务条目合并到本机 `mcp.local.json`。把示例租户、空间、用户替换为实际登录身份。API 和 Worker 的本机环境须设置相同的 `AGENTFORGE_MCP_COMPUTER_TOKEN`，以及指向该 JSON 文件的绝对路径 `AGENTFORGE_MCP_SERVERS_FILE`。重新加载配置后，通过现有 MCP 发现接口获取工具 reference 和摘要。示例 HTTP 地址仅用于同机回环，跨主机部署配置内部 HTTPS。

5. 依照 [MCP 工具绑定](../../docs/mcp.md) 将发现的工具绑定到 Chat Completions Agent。要使用审批卡片，把该工具的 reference 同时加入 `approval_tools`。在原版 Rakazo 聊天里发送“请调用统计工具计算 -3、6、15 的总和与均值”。允许本次后，工具记录应包含 `total=18` 和 `mean=6.0`。查看 `GET /api/v1/runs/{run_id}/tool-calls` 可核对实际输入与结果。

## 在原版 Rakazo 中导出 CSV

1. API 和 Worker 使用同一版本，按现有迁移流程将数据库升级到 Alembic `0014`。
2. 在 Computer 的 `read_tools` 中启用 `export_number_summary_csv` 版本 1。发现工具后，将该工具绑定到 Agent，并把返回的完整 reference 加入 `approval_tools`。
3. 在聊天里发送“请调用 CSV 导出工具，统计 -3、6、15 并生成 CSV 附件”。在审批卡片中选择允许。聊天附件和文件页均可获取 `number-summary.csv`，数值行为 `3,-3,15,18,6.0`。
4. 同一调用的报告、附件消息和执行结果在一个事务内保存。Worker 重启后复用已保存的结果。每次新的导出独立生成一个文件。

## 执行边界

单进程最多同时运行两个计算，超出时调用失败。容器使用非 root 用户、只读根文件系统、禁用网络、移除 Linux capabilities、禁用权限提升及健康检查，限制 0.5 CPU、64 MiB 内存、16 个进程。单次调用使用随机容器名，固定镜像 ID 且禁止自动拉取。容器不挂载宿主目录、数据库或 Docker socket。仅 Supervisor 进程访问 Docker。

正常结束由 Docker 自动删除容器。超过 12 秒或调用失败时尝试按本次容器名强制删除，容器内的程序另有 8 秒截止时间。取消 Run 会阻止过期结果写回，已开始的计算可能持续到完成或超时。Docker 故障期间回收可能延迟。当前只支持固定数值计算。

## 验证

`tests/test_computer.py` 默认验证审批、参数边界、HTTP 身份、并发上限、超时清理、结果回传和审计，使用模拟模型与模拟 Docker。要在独立测试库中执行真正的容器计算，可在已获授权的环境指定完整本地镜像 ID 和 Docker 上下文：

```bash
DOCKER_CONTEXT=colima AGENTFORGE_TEST_COMPUTER_IMAGE="${AGENTFORGE_COMPUTER_IMAGE:?请先导出本地镜像的完整 ID}" uv run --locked pytest tests/test_computer.py::test_chat_approval_to_computer_and_back
```

此用例通过进程内 MCP HTTP 传输连接真实服务应用，模型使用测试响应，不会调用付费模型。

## 为开发助手打开专属 Linux 桌面

在单机开发环境中，可将一个图形桌面分配给指定租户、空间、用户和 Bot。先准备含 Xvfb、Fluxbox、xterm、x11vnc 和 Rakazo noVNC 静态文件的固定镜像。设置好上述 Docker 上下文与镜像 ID 后，在独立终端运行以下命令，并用实际身份替换占位值。

```bash
desktop_dir=$(mktemp -d)
echo "$desktop_dir/desktop.json"
uv run --locked --env-file .env.supervisor python -m agentforge_supervisor.desktop \
  --tenant TENANT --space SPACE --user USER --bot BOT_UUID --manifest "$desktop_dir/desktop.json"
```

启动 API 时把 `AGENTFORGE_DESKTOP_MANIFEST` 指向打印的绝对路径。为 API 与 Rakazo Web 设置相同的专用随机 `SCREEN_PROXY_SECRET`，至少 32 字符，保存在本地可信环境。单进程 API 管理临时屏幕授权，Web 使用自带的 noVNC 代理。桌面由独立 Supervisor 启动，API、Worker 不需要 Docker 权限。

使用被分配账号登录，在该助手的「电脑」面板查看画面，点接管后可在 Linux 终端操作键鼠。释放接管、撤销登录或更换桌面会使原控制链接失效。桌面断网、无宿主机目录挂载，临时文件随容器退出清除。结束 Supervisor（Ctrl+C）可关闭桌面，单次最长运行四小时。此桌面独立于执行 CSV 的临时容器。

## 当天财经新闻

独立的只读 MCP 服务读取中新网财经 RSS，按北京时间筛选当天已发布的新闻，最多返回最近五条标题、原文链接及发布时间。关键词仅在本地筛选标题。抓取失败会返回工具错误。

给新闻服务、API 和 Worker 设置同一个专用随机令牌 `AGENTFORGE_MCP_NEWS_TOKEN`。启动新闻服务：

```sh
uv run --locked uvicorn agentforge_supervisor.finance_news:create_app --factory --host 127.0.0.1 --port 8767
```

将 `infra/computer/news.example.json` 的条目合并入当前 MCP 配置，替换授权身份，重新加载 API 与 Worker。发现并绑定 `finance_news/latest_finance_news`，保留助手已有工具与审批规则。聊天验收可发送“帮我检索今天最新的财经新闻”。

### 在原版「电脑」中查看财经检索

可为一台已分配的开发桌面启用独立的 `agentforge_supervisor.visible_news:create_app` MCP 服务。使用同一个 `AGENTFORGE_DESKTOP_MANIFEST`；API 和该 Supervisor 共享专用的 `AGENTFORGE_DESKTOP_READER_TOKEN`，API、Worker 和该服务共享另一个专用的 `AGENTFORGE_MCP_SCREENNEWS_TOKEN`（均至少 32 字符）。本机示例服务端口为 8768，API 为 8000。部署者为该服务的 MCP 配置指定桌面对应的 tenant_id、space_id、单一 users 和 bot_id，绑定 `browse_finance_news` 版本 1，超时建议 45 秒。执行器会校验 Bot 绑定。

使用查看模式发送财经新闻请求。Agent 在无头模式关闭的 Chromium 中访问中新网财经频道，读取网页 DOM 的时间和标题，滚动并点击最新匹配条目，成功打开原文后返回链接。Supervisor 仅为该标签页代理固定财经频道和一篇原文的 GET 请求，验证上游 HTTPS；网页脚本、表单和外部资源被限制。用户持有键鼠控制权时，需先交还才能检索。

### 按需启动与回收专属桌面

本机单进程 API 可管理多个桌面名额。每个名额使用独立的私有目录（0700）和一个轻量 Supervisor 进程，在用户点击启动前不占用 Docker 容器。API 与 Supervisor 以同一主机账号通过本地 Unix socket 通信。

1. 为每个名额创建短路径的私有目录。为该用户和 Bot 运行上述桌面命令，追加 `--on-demand`，将 `--manifest` 指向该目录内新的 `desktop.json`。保留目录供 Supervisor 重启时回收上次中断的容器。
2. 仅对 API 设置 `AGENTFORGE_DESKTOP_SLOTS` 指向 JSON 名册，取代 API 的 `AGENTFORGE_DESKTOP_MANIFEST`。数组中每个条目对应一条 Supervisor 命令，例如：

   ```json
   [{"tenant":"local","space":"development","user":"developer","bot":"00000000-0000-4000-8000-000000000001","manifest":"/tmp/af-slot-a/desktop.json"}]
   ```

3. 使用对应账号在 Bot 的「电脑」面板启动桌面。重复启动沿用当前桌面。原版 oRPC 的 `computer.stop({ botId })` 会销毁容器和临时文件，并使这一代桌面的画面链接与控制授权失效。下次启动得到全新桌面。单个桌面最长运行四小时。若启用可见财经检索，仍给该工具配置对应名额的 manifest。
4. 双账号验收时，在 A 的终端创建临时文件，再用 B 登录另一 Bot，检查其桌面没有该文件。让两个账号分别接管，停止 A 后验证 B 仍可输入，A 的旧链接不能连接。这一部署模式按配置分配名额，不跨主机调度。

### 在原版 Files 面板中操作桌面文件

启用上述按需桌面，重启使用新代码的 API 和对应 Supervisor，再启动该 Bot 的桌面。打开「电脑 → Files」，可列目录、预览 UTF-8 文本和图片、下载文件。点击接管后可上传或覆盖文件。只有当前持有键鼠控制权的浏览器登录会话可上传，交还控制权后仍可查看。文本预览上限为 2 MiB，单文件传输上限为 10 MiB，每个目录最多列出 1024 项。

手工验收时，上传一份中文文本，再在桌面 xterm 中运行 `ls -l /workspace`，编辑后回到 Files 查看并下载。工作目录是该容器独有的 64 MiB 临时挂载，停止并重新启动桌面会清空它。聊天中已持久化的 CSV 仍从聊天附件下载。独立 Terminal 面板的交互式 Shell 尚待接入。

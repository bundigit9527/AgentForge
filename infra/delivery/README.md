# 单机 Ubuntu 的 Docker 交付

交付端使用 Docker Engine 和 Compose v2.24+，数据库使用已有的 PostgreSQL。Web 使用 Rakazo 原版应用镜像中的 Web 入口。API 与 Worker 使用 AgentForge 后端镜像。Supervisor 使用带 Docker CLI 的单独镜像，并按需向宿主机 Docker Engine 创建无网络的 Computer 容器。

## 准备镜像

在与目标服务器同架构的构建机上，从 AgentForge 仓库根目录执行：

```sh
docker build -f infra/compose/Dockerfile --target backend -t agentforge/backend:local .
docker build -f infra/compose/Dockerfile --target supervisor -t agentforge/supervisor:local .
```

若构建机连不上 Docker Hub，可给上面两条命令加上 `--build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm --build-arg CLI_IMAGE=public.ecr.aws/docker/library/docker:28-cli`。在目标架构的机器上构建，或用 Buildx 指定目标平台。

Terminal 的前端验收基线是 Rakazo `d165de43`。请使用包含 `apps/web/src/components/computer/TerminalApp.tsx` 的对应版本构建 Web，并将 `RAKAZO_WEB_IMAGE` 指向该镜像。下面的命令在该 Rakazo 仓库根目录运行：

```sh
docker build -f infra/compose/Dockerfile -t rakazo/web:local .
docker build -t rakazo/computer:local infra/sandboxes/computer
```

也可以使用已验证兼容的上游预构建镜像。对外交付时将四个镜像推到客户可访问的 Registry，或用 `docker save -o agentforge-images.tar agentforge/backend:local agentforge/supervisor:local rakazo/web:local rakazo/computer:local` 导出，在 Ubuntu 上用 `docker load -i agentforge-images.tar` 导入。交付目录是本目录的文件，接收端不需要 Python、Node.js 或源码。为正式版本固定 tag 或 digest，记录两个仓库的提交号。

## 配置与启动

在 Ubuntu 的交付目录中：

```sh
umask 077
cp settings.example .env
cp backend.example .env.backend
mkdir -p config
openssl rand -hex 32
stat -c '%g' /var/run/docker.sock
docker image inspect --format '{{.Id}}' rakazo/computer:local
```

将三条命令的输出分别填入 `.env` 的 `SCREEN_PROXY_SECRET`、`DOCKER_SOCKET_GID`、`AGENTFORGE_COMPUTER_IMAGE`。在 `.env.backend` 填外部 PostgreSQL URL（使用容器可访问的数据库地址）、Web 的实际访问来源，以及需要的真实模型配置。域名部署时 `RAKAZO_HOST` 填主机名，`AGENTFORGE_WEB_ORIGIN` 填带协议的完整来源。默认只发布回环端口 5173，外部访问由现有 HTTPS 反向代理转发，启用 WebSocket 升级。

```sh
docker compose config --quiet
docker compose run --rm migrate
docker compose up -d
docker compose ps
```

迁移命令显式升级到 0015。首次部署在 API 容器内创建管理员并设置密码，参数见 [身份指南](../../docs/identity.md)：

```sh
docker compose exec api agentforge-admin bootstrap --tenant local --tenant-name 公司 --space development --space-name 开发 --user admin --display-name 管理员
docker compose exec api agentforge-admin set-password --tenant local --space development --user admin --email admin@example.com
```

原版 Web 通过内部网络访问 API。API 使用私有 Unix socket 向 Supervisor 发请求。Supervisor 和 Web 共享网络命名空间，原版屏幕代理可访问回环 noVNC 转发端口。Docker socket 只挂给 Supervisor 服务（启用可选计算时也挂给计算 Supervisor）。部署规格为一个 API 进程、一个桌面池，默认最多四台运行中的电脑，可调整 `AGENTFORGE_DESKTOP_LIMIT`。Worker 可通过 `docker compose up -d --scale worker=2` 水平扩展。

可选 CSV/数值计算：使用 [Computer MCP 配置示例](../computer/servers.example.json) 创建 `config/mcp.json`，将 URL 改为 `http://compute:8766/mcp` 并替换授权身份。将同一专用令牌填入 `.env.compute` 和 `.env.backend` 的 `AGENTFORGE_MCP_COMPUTER_TOKEN`，设置 `AGENTFORGE_MCP_SERVERS_FILE=/etc/agentforge/mcp.json`，再用 `docker compose --profile compute up -d` 启用。工具发现、绑定和审批按原流程执行。

## 验收 Team / Private

1. 在同一空间创建两个 Team Bot。分别启动电脑，两个面板应看到同一桌面。一个 Bot 接管时，另一个 Bot 的接管请求应返回冲突。
2. 交还控制权，在其中一个 Bot 的设置 → Advanced → Computer 选择 Private 并保存。新启动的 Private 桌面应独立，另一个 Bot 的 Team 桌面仍在运行。切换后旧屏幕链接失效。
3. 停止 Private 桌面后切回 Team。进行中的 Run 和未释放的控制租约会阻止切换。正常停止会保存当前模式的 /workspace 并回收桌面容器。再次启动同一模式会恢复原文件；Team 与 Private 之间切换时，各自的文件留在原作用域。
4. Team 按租户和空间复用电脑。成员经自己有权操作的 Bot 使用共享屏幕；Files 面板默认定位到各 Bot 的工作目录。共用同一桌面的成员可通过终端访问该电脑里的其他文件，机密工作使用 Private。迁移前的 Bot 保留 Private，新建 Bot 默认 Team。
5. 本版在手动启动电脑时分配容器，聊天仍运行在 Python Harness。沙箱单次最长运行四小时，不提供跨宿主机迁移。独立 Terminal 支持交互式 Shell。接管后执行 `pwd; echo hello > terminal-test.txt`，在 Files 中预览该文件，再交还控制权验证连接断开。Rakazo 的空闲检查点策略仍待对齐。

2026-11-04 在 Colima Linux 容器环境完成 Compose 验收，使用独立 PostgreSQL 和原版 Web 镜像 `ghcr.io/elie222/rakazo/app@sha256:ddafd94786b92193b62effdce6bbc03e19c38b44d383402ea88a81c3edb70ae7`。该摘要记录的是上一版 Web 代理验收。通过 Web 代理验证了双账号密码登录、共享电脑接管互斥、上传下载、noVNC 页面、Private 切换及旧链接撤销。目标 Ubuntu 服务器仍需用现场数据库、域名和目标架构镜像联调。

## 桌面池的工作区保存与恢复

Compose 将名为 `workspace_checkpoints` 的持久卷挂到 Supervisor 的 `/checkpoints`，仅 Supervisor 可读取归档。每个桌面运行时仍使用 64 MiB、nosuid/nodev/noexec 的内存工作区。归档键沿用桌面分配键，Team 按租户和空间保存，Private 还包含用户与 Bot。每个键仅保留最近一次完整归档。

- **正常停止**会先冻结桌面，通过只读辅助容器保存文件并原子替换旧归档，再移除桌面。保存失败时，停止请求返回冲突，桌面继续运行，可修复磁盘空间或权限后重试。
- **Files 上传**在新内容成功归档后返回成功。Terminal 或桌面应用写入的文件在后台定期保存：通常从上一次保存后约 30 秒开始，多个桌面依次处理。突然断电、桌面容器丢失或四小时硬超时后，可恢复到最后一次成功归档，较新的未归档修改可能丢失。Supervisor 异常退出而桌面仍存活时，重启会先保存遗留桌面的工作区再回收容器。
- **恢复范围**为 `/workspace`，包括 Team 各 Bot 的工作目录。浏览器登录态、Shell 进程、`/tmp` 和打开的窗口不随文件恢复。停机期间不能通过 Files 浏览归档。
- **运维**需要监控持久卷的可用空间，按需备份该卷。单次归档最多 128 MiB；单工作区的归档上限不会限制所有空间的总占用。`docker compose down` 保留检查点卷，`docker compose down -v` 会删除它。升级仍在运行的旧版时，请先导出旧临时桌面中的重要文件。

验收时，在 Team 的 Terminal 中创建 `note.txt`，再在 Private 中创建内容不同的同名文件。分别停止、重启并检查文件内容。另建一个空间并启动它的 Team 桌面，确认原空间的工作目录不在其中。

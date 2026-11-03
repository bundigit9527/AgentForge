# Docker Computer 计算服务

Supervisor 是独立运行的 Python 服务。首个工具 `summarize_numbers` 接受 1–512 个整数，单值范围为 ±10¹²，返回数量、最小值、最大值、总和与均值。每次调用在新容器中运行固定 Python 程序。用户数据从标准输入传入，程序、镜像与 Docker 参数由部署者控制。

Worker 通过现有 Streamable HTTP MCP 连接器发现和调用工具。租户、空间、用户授权、运行租约及工具定义核对由 AgentForge 执行，Supervisor 单独验证服务令牌。该令牌可直接调用计算服务，应仅分发给受信的服务进程。当前没有逐 Run 签发的容器凭证、持久工作目录或产物下载能力。

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

## 执行边界

单进程最多同时运行两个计算，超出时调用失败。容器使用非 root 用户、只读根文件系统、禁用网络、移除 Linux capabilities、禁用权限提升及健康检查，限制 0.5 CPU、64 MiB 内存、16 个进程。单次调用使用随机容器名，固定镜像 ID 且禁止自动拉取。容器不挂载宿主目录、数据库或 Docker socket。仅 Supervisor 进程访问 Docker。

正常结束由 Docker 自动删除容器。超过 12 秒或调用失败时尝试按本次容器名强制删除，容器内的程序另有 8 秒截止时间。取消 Run 会阻止过期结果写回，已开始的计算可能持续到完成或超时。Docker 故障期间回收可能延迟。当前只支持固定数值计算。

## 验证

`tests/test_computer.py` 默认验证审批、参数边界、HTTP 身份、并发上限、超时清理、结果回传和审计，使用模拟模型与模拟 Docker。要在独立测试库中执行真正的容器计算，可在已获授权的环境指定完整本地镜像 ID 和 Docker 上下文：

```bash
DOCKER_CONTEXT=colima AGENTFORGE_TEST_COMPUTER_IMAGE="${AGENTFORGE_COMPUTER_IMAGE:?请先导出本地镜像的完整 ID}" uv run --locked pytest tests/test_computer.py::test_chat_approval_to_computer_and_back
```

此用例通过进程内 MCP HTTP 传输连接真实服务应用，模型使用测试响应，不会调用付费模型。

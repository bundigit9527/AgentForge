# Python 自动文件生成与 Rakazo 对齐

## 本轮实现

原版 Rakazo Web 发送普通聊天 → API 固定 Python 镜像与可执行 Skill 目录 → Worker 中既有 Python Harness 读取 Skill → `python_execute` → 计算 Supervisor 创建独立 Docker 容器 → 校验产物 → 工具记录、附件和消息事件在同一事务保存。

后端仍为 Python。没有增加第二个 Agent 循环，没有改动 Rakazo Web，也不要求用户输入斜杠命令。内置「PPT生成」提供内容规划、可编辑对象、结构检查和失败修正说明。主题可从已有对话确定时直接执行；没有主题时仍需用户补充。普通聊天由模型选择技能，不保证模型每次遵循步骤；可通过 `/PPT生成` 固定技能读取。

`python_execute` 接收 `code` 和 `exports`。预装 python-pptx、openpyxl、python-docx、Pillow；可生成 PPTX、XLSX、DOCX、PDF、PNG/JPG、CSV、TXT、Markdown、JSON、ZIP。预装库不等于每种格式都有完整的排版 Skill；本轮新增的配方只有 PPT。结构检查不代表逐页视觉检查。

## 执行和授权边界

- 默认关闭。只为策略明确授权的租户、空间、用户启用；选择 Skill 不会授权执行。旧 Run 缺少 `python_image` 时不获得新工具。
- API 新建 RPC Run 时固定镜像 ID，再解析 Skill 依赖；重试复用原快照。REST 创建的历史 Agent 配置不会自动获得能力。
- Worker 在执行检查点、请求前后核对当前成员、任务租约、取消、授权和镜像。策略撤销后停止新操作，结果不能发布；已交付历史附件仍按原有归属授权读取。
- Worker 直接调用现有计算 Supervisor；只有 Supervisor 管理 Docker。API、Worker、Computer 均不挂 Docker socket。
- 每次调用使用全新的离线容器，无宿主机目录、凭据或桌面挂载。只读根目录、非 root、移除 capabilities、禁止提权；1 CPU、384 MiB 内存、32 个进程、45 秒脚本期限；工作区为 64 MiB 内存目录。
- stdout/stderr 各返回最多 3000 字符。最多 4 个文件、合计 10 MiB；只允许普通文件和简单文件名，拒绝符号链接及目录穿越。Office 文件校验 ZIP 结构和必要成员。
- 文件二进制不放进模型上下文或工具 JSON，单独保存到当前用户的聊天附件。工具缓存重放不再次执行或重复发布附件；取消、失效租约不能写入结果。
- 脚本非零退出以 exit_code/stderr 返回模型，由同一 Harness 修正重试；基础设施故障、撤权等不会伪造成功。客户端断开或 Worker 检测取消后关闭请求，Supervisor 清理容器；崩溃时容器自身还有期限。

## 本地配置

构建执行镜像（macOS 使用 Colima，Ubuntu 去掉 `--context colima`）：

```bash
docker --context colima build -f infra/computer/python.Dockerfile -t agentforge/python:local infra/computer
docker --context colima image inspect agentforge/python:local --format '{{.Id}}'
```

构建可通过 `--build-arg PYTHON_IMAGE=<已有可信 Python 3.12 基础镜像>` 替换基础镜像。生产固定基础镜像摘要；运行时只接受本机已安装的 sha256 镜像 ID，不自动拉取或安装包。

在本机 `.env` 配置：

```dotenv
AGENTFORGE_PYTHON_POLICY_FILE=/absolute/private/python-policy.json
AGENTFORGE_PYTHON_IMAGE=sha256:<上一步的镜像ID>
AGENTFORGE_PYTHON_ENDPOINT=http://127.0.0.1:8766
AGENTFORGE_MCP_COMPUTER_TOKEN=<独立的随机服务令牌，至少32字符>
```

策略文件：

```json
{"grants":[{"tenant_id":"实际租户","space_id":"实际空间","users":["实际用户ID"]}]}
```

API、Worker、计算 Supervisor 共用配置，令牌只用于后端服务通信，不传入 Computer 或前端。计算服务沿用数值计算服务入口，因此还需已有的 `AGENTFORGE_COMPUTER_IMAGE`。分别在终端启动：

```bash
uv run --env-file .env --locked --offline python scripts/dev_service.py compute
uv run --env-file .env --locked --offline python scripts/dev_service.py api
uv run --env-file .env --locked --offline python scripts/dev_service.py worker
```

Docker Compose：构建并交付额外的 Python 执行镜像；在 `.env` 和 `.env.backend` 填同一 `AGENTFORGE_PYTHON_IMAGE`，`.env.backend` 设置策略路径与 `AGENTFORGE_PYTHON_ENDPOINT=http://compute:8766`；同一服务令牌放入 `.env.backend`、`.env.compute`。策略文件放 `config/`，启用现有 `compute` profile。不发布计算服务公网端口。

## 与本地 Rakazo 源码的差异

核查版本：Rakazo `9218c401329b8626bc4d4b9ffde0034ea97a37f0`，原 Web 兼容基线仍为 `d165de43`。

| 能力 | Rakazo 本地源码 | AgentForge 本轮 |
| --- | --- | --- |
| 执行循环 | Pi 驱动模型、工具和后续轮次 | 原有 Python Harness，沿用任务、租约和工具日志 |
| Skill | 目录与 `skill_read`；内置清单只有 Interrogate | 新增「PPT生成」；自动目录发现，按需读取 |
| 执行代码 | `write_file` + `shell`，在 Bot Computer 上操作 | `python_execute`，执行完整 Python 脚本 |
| 目录生命周期 | Team/Private Computer 持久工作目录 | 每次执行为独立临时目录；不复用桌面文件与登录态 |
| 依赖 | Computer 含 Python、pandoc、uv 等通用程序 | 专用执行镜像预装 Office 生成依赖；运行时不安装 |
| 文件交付 | `attach_file` 读取工作目录并发布附件 | `exports` 显式指定文件；执行成功后原子发布 |
| PPT 附件 | 当前附件扩展名清单没有 `.pptx`；不能声称直接完整支持 | 增加 Office MIME，沿用原版 Web 通用 file 消息和下载 |
| 可见桌面 | 可观察桌面程序与持久文件操作 | 本轮代码生成不显示在桌面，也不自动打开 PowerPoint |
| 错误处理 | 工具结果交回执行循环 | 脚本错误交回同一 Harness；有限轮次内修正 |

依据：Rakazo `packages/adapters/src/builtin-tools.ts`、`builtin-skills.ts`、`executor.ts`、`pi-runtime.ts`、`packages/core/src/attachments.ts`、`infra/sandboxes/computer/Dockerfile`。

后续对齐：按 Bot/Team 受控挂接持久工作目录、读写已有附件/资源包、独立文件发布工具、桌面打开文件与渲染验收。不能把本轮称为已经完整对齐通用 Shell 或 Computer。

## 验收

普通聊天输入：`把上面的公司 AI 平台方案生成 5 页可编辑 PPT，直接生成文件。`

应观察到读取 PPT Skill、Python 工具执行、实际 `.pptx` 附件；下载后在 PowerPoint 中编辑文本和形状。脚本报错应有具体 stderr 并继续修正；取消后不得发布后到的附件。其他账号不能下载该私有附件。当前不依赖先上传文件。

自动回归见 `tests/test_python_execution.py`；真实 Docker 和真实模型验收应分别报告，不把 Mock 测试当成真实生成。

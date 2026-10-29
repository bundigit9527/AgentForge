# 开发入门

当前提供 Python 后端骨架和离线任务闭环。代码已包含 API、单次 / 持续运行 Worker、模拟模型、内置报告工具、提交去重、数据库任务队列及 SSE 实时事件；真实模型、企业登录、长期记忆、Docker 沙箱、Web 和桌面端尚未接入。

## 环境与安装

使用 Python 3.12 和 uv。依赖范围由 `pyproject.toml` 管理，具体版本记录在 `uv.lock`；首次安装需要访问依赖源。

在 AgentForge 项目目录运行：

```bash
uv sync --locked
```

这一步仅建立项目虚拟环境并安装依赖，不启动 API、Worker、数据库或容器。开发配置以 `.env.example` 为模板，复制为已被 Git 忽略的 `.env`，通过 `--env-file .env` 显式加载；应用本身不自动读取该文件。

## macOS 容器环境：Colima

本项目的 macOS 开发流程使用 Colima 默认 profile 的 Docker 运行时。先检查状态和当前 Docker 上下文：

```bash
colima status
docker context show
```

如果 Colima 未运行，需要联调时再手动启动：

```bash
colima start --runtime docker
```

启动后确认 Docker 服务和 Compose 命令可用：

```bash
docker --context colima version
docker --context colima compose version
```

下文的容器命令都显式指定 `--context colima`，使用该环境中的镜像、容器和数据卷；不同 Docker 环境的数据卷独立管理，切换上下文不会自动搬迁数据库。本项目的本机 API / Worker 通过 `127.0.0.1:55432` 连接 PostgreSQL。

以上启动操作仅供手动调试时执行。离线测试无需启动 Colima；后续 Ubuntu 服务器部署采用 Docker Engine。Colima 运行时与上下文说明见 [官方文档](https://colima.run/docs/faq/)。

## 离线验证

安装完成后运行：

```bash
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
uv run --locked --offline pytest
```

测试使用进程内 HTTP 客户端和临时文件 SQLite 数据库，不监听网络端口，不启动服务、容器或真实模型。数据库在测试临时目录中通过 Alembic 迁移建立。

测试覆盖：任务提交去重与结果持久化、事件流与续传、取消、租户/空间/用户访问范围、重复领取、执行租约续期与失效、持续消费、重试上限、迁移保留已有数据，以及任务状态与事件的事务一致性。SQLite 验证不代表 PostgreSQL 或生产环境验证已经完成。

已提供显式启用的 PostgreSQL 集成测试模式，复用上述行为测试，并增加独立连接和并发验证。它只读取 `AGENTFORGE_TEST_DATABASE_URL`，要求专用测试库，每个数据库用例使用独立临时 schema；默认测试命令仍为 SQLite。配置与操作步骤见 [PostgreSQL 集成测试](postgresql-testing.md)。

## 代码从哪里看

| 路径 | 当前内容 |
| --- | --- |
| `apps/api/agentforge_api/app.py` | FastAPI 应用工厂、开发认证、任务及事件接口 |
| `apps/api/agentforge_api/streaming.py` | SSE 事件编码、续传、心跳与断线清理 |
| `apps/worker/agentforge_worker/worker.py` | 领取并执行一个任务，保存结果或失败状态 |
| `apps/worker/agentforge_worker/lease.py` | 后台租约续期与失效检测 |
| `packages/core/agentforge_core` | 身份范围、任务状态、领域对象和配置 |
| `packages/contracts/agentforge_contracts` | 请求与响应结构，拒绝客户端注入身份与任务状态 |
| `packages/storage/agentforge_storage` | SQLAlchemy 数据定义和事务化任务队列 |
| `packages/runtime/agentforge_runtime/harness.py` | 模型接口、模拟决策和白名单内置工具 |
| `migrations` | Alembic 数据库迁移；应用启动不自动建表 |
| `tests` | 离线行为与迁移验证 |

首版用一个 Python 发行包管理这些模块，通过根目录显式配置映射源目录；导入名分别为 `agentforge_api`、`agentforge_worker`、`agentforge_core` 等。后续是否拆成多个发行包取决于独立发布需要。

## 当前接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health/live` | 进程存活检查，不连接数据库 |
| `GET /health/ready` | 检查数据库、表结构可读及迁移版本匹配 |
| `POST /api/v1/runs` | 提交 `{"prompt":"生成一份示例报告"}`，可带 `Idempotency-Key` |
| `GET /api/v1/runs/{run_id}` | 查询状态与结果 |
| `POST /api/v1/runs/{run_id}/cancel` | 取消排队或执行中的任务 |
| `GET /api/v1/runs/{run_id}/events?after=0&limit=100` | 按运行内单调游标读取已持久化事件 |
| `GET /api/v1/runs/{run_id}/events/stream?after=0` | SSE 实时事件，支持 `Last-Event-ID` 续传 |

任务接口需要 Bearer 令牌。令牌只映射到服务端配置的一组开发身份，尚未实现企业登录、用户管理或团队共享授权。未配置令牌时任务接口返回 503；令牌无效返回 401；不存在或无权限的任务统一返回 404。重复取消已取消任务返回当前状态，取消已成功或失败的任务返回 409。

任务尚未关联 Agent 目录、会话或工作流，固定使用模拟模型和内置示例工具。使用提交去重键可以避免网络重试产生新任务；同一身份范围内复用键但改变任务内容会返回 409。事件分页和 SSE 接口并存，详细约定见 [任务执行协议](task-execution.md)。

## 数据与恢复

- 首版由数据库任务表兼任持久化队列，任务提交与事件写入在同一事务中完成，不存在数据库与外部消息队列的双写。
- Worker 用条件更新领取任务，默认租约 60 秒，后台每 20 秒续期；执行尝试通过独立标识隔离，过期或取消的尝试不能提交后续工具事件和最终结果。
- Worker 中断后，租约过期的任务可以重新领取，最多尝试三次。只有当前无外部副作用的模拟工具允许自动重做。
- 已实现后台续租与任务事件流；模型 token 流式输出、审批暂停及外部操作补偿仍待实现。真实模型、长任务或外部业务工具接入前需补齐相应机制。
- 取消阻止后续状态写入；正在计算的同步模拟函数不会被强制杀死。未来沙箱进程和模型请求需要独立的取消适配。
- 结果作为文本保存在数据库中；文件产物、知识库和长期记忆尚未实现。

生产目标仍为 PostgreSQL。已声明 PostgreSQL 驱动和迁移代码，并已在独立 PostgreSQL 17.11 测试库验证迁移、并发、应用层隔离与租约恢复。生产账号权限、网络服务联调、容量与备份恢复仍需单独验证。

## 本地 PostgreSQL 开发容器

当前开发方式为“本机 Python API / Worker + Colima 中的 PostgreSQL 容器”。[compose.dev.yaml](../infra/compose/compose.dev.yaml) 只包含数据库，使用 `postgres:17.11-bookworm`，绑定 `127.0.0.1:55432`，数据库为 `agentforge_dev`，开发账号为 `agentforge`。数据库数据保存在 Colima Docker 环境中的独立卷中。

以下命令用于手动开发联调；自动化集成测试使用另一套独立配置。先从仓库根目录准备 `.env`；如果文件已存在，保留原配置并手动合并本地数据库字段：

```bash
cp -n .env.example .env
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml config --quiet
```

配置校验不连接 Docker 数据库，也不下载或启动镜像。准备好联调时执行：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml up -d --wait postgres
uv run --env-file .env --locked alembic upgrade head
```

第一条命令启动数据库并等待健康检查通过；第二条命令创建 `runs`、`run_events` 和 `alembic_version`。`.env.example` 内的数据库 URL 已与容器端口、账号和密码对应。若更改端口或密码，需同步修改 URL；密码包含特殊字符时，对 URL 内密码进行编码。终端中已有的同名环境变量可能覆盖 `.env`，执行迁移前应确认目标仍为本地开发库。

开发数据库仅用于本机联调，模板密码不用于生产。官方镜像只在空数据目录中执行账号初始化；修改 `.env` 不会重置已有卷中的账号密码。正常停止并保留数据：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml down
```

此处不使用删除卷的选项。默认自动化测试使用临时 SQLite，显式 PostgreSQL 模式已通过独立测试容器验证，不会使用这里的开发数据库。完整参数见 [开发数据库说明](../infra/compose/README.md)；镜像初始化与卷规则参考 [PostgreSQL 官方镜像](https://hub.docker.com/_/postgres)。

## 后续接入已有的外部 PostgreSQL

开发阶段先使用上一节的本地容器。后续服务器环境可将已有外部 PostgreSQL 作为 API 与 Worker 的共同存储，无需额外部署服务器侧数据库容器。当前代码已包含 `psycopg` 驱动和 PostgreSQL 连接适配，尚未连接或验证外部实例。

通过服务进程的环境变量 `AGENTFORGE_DATABASE_URL` 设置连接；下例所有地址、用户名和密码均为占位值：

```text
postgresql+psycopg://agentforge_user:URL_ENCODED_PASSWORD@db.example.internal:5432/agentforge?connect_timeout=10
```

API、Worker 和迁移工具应指向同一目标数据库。容器中的 `localhost` 指向容器自身；请填写从 Ubuntu 后台容器可访问的外部数据库地址。密码中的特殊字符需进行 URL 编码，TLS 参数按现有实例要求配置；使用证书校验时，运行环境还需提供相应 CA 文件。参数说明见 [SQLAlchemy PostgreSQL 驱动](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#psycopg) 和 [PostgreSQL 连接参数](https://www.postgresql.org/docs/current/libpq-connect.html)。

接入前需要明确：

- 数据库地址、端口、库名和 PostgreSQL 版本。
- 是否为 AgentForge 专用开发/测试库，以及可进行的迁移操作。
- 连接账号、TLS/证书要求，以及当前电脑和 Ubuntu 服务器的网络可达性。

初次接入优先使用已有实例上的 AgentForge 专用开发/测试库与账号。当前迁移使用连接的默认 schema，会创建 `runs`、`run_events` 和 `alembic_version`；共享业务库下的独立 schema 尚未配置，需明确 schema 与权限后再适配。默认离线测试使用临时 SQLite；显式集成测试只读取独立测试连接变量，并把迁移及降级限定在每用例临时 schema，不能使用业务库运行。

数据库信息确认后，先检查连通性、版本及当前结构，再在指定开发/测试库执行 `uv run --locked alembic upgrade head`。该命令会修改数据库结构；应用启动和创建项目本身均不会自动执行它。不要将现有测试直接改为对共享业务库运行。

## 后续手动启动 API 与 Worker

完成本地数据库启动与迁移后，将 `.env` 中的 `AGENTFORGE_DEV_TOKEN` 设置为自选随机令牌（至少 32 个字符）。`AGENTFORGE_DEV_TENANT`、`AGENTFORGE_DEV_SPACE`、`AGENTFORGE_DEV_USER` 指定本地开发身份。以下命令供后续需要调试时使用，本次不执行：

```bash
uv run --env-file .env --locked uvicorn agentforge_api.app:create_app --factory --host 127.0.0.1 --port 8000
```

API 启动后通过 `/docs` 查看接口并设置 Bearer 令牌。提交任务后，另一个终端在仓库根目录加载同一份 `.env`，执行一次 Worker：

```bash
uv run --env-file .env --locked agentforge-worker --once
```

`--once` 每次领取最多一个任务后退出。后续需要持续处理队列时可手动使用 `--watch`，支持租约续期和退出信号处理；本次没有启动常驻进程。完整参数与数据库 `0002` 升级说明见 [任务执行协议](task-execution.md)。

实现参考：[FastAPI 测试文档](https://fastapi.tiangolo.com/tutorial/testing/)、[SQLAlchemy 事务文档](https://docs.sqlalchemy.org/en/20/core/connections.html)、[Alembic 迁移文档](https://alembic.sqlalchemy.org/en/latest/tutorial.html)。

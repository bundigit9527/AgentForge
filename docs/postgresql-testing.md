# PostgreSQL 集成测试

测试有两种模式：默认的 SQLite 离线验证，以及显式启用的 PostgreSQL 集成验证。两者复用 API、任务队列、租约、幂等、事件流和迁移测试；PostgreSQL 模式另外验证独立连接、并发消费、取消与完成竞争、结果持久化和临时 schema 清理。

## 默认离线模式

安装依赖后，在项目根目录运行：

```bash
uv run --locked --offline pytest
```

此模式仅使用临时 SQLite 文件。即使环境中设置了应用或测试数据库地址，也不会自动连接 PostgreSQL。仅适用于 PostgreSQL 的测试显示为跳过。

## 独立测试环境

| 项目 | 开发联调 | 自动化集成测试 |
| --- | --- | --- |
| Compose 文件 | `infra/compose/compose.dev.yaml` | `infra/compose/compose.test.yaml` |
| Compose 项目 | `agentforge-dev` | `agentforge-test` |
| 默认本机端口 | `55432` | `55433` |
| 数据库 | `agentforge_dev` | `agentforge_test` |
| 配置模板 | `.env.example` | `.env.test.example` |
| 连接变量 | `AGENTFORGE_DATABASE_URL` | `AGENTFORGE_TEST_DATABASE_URL` |
| 数据保存 | 持久化数据卷 | 临时内存文件系统，停止容器即丢弃数据库 |

测试模板中的账号和密码仅用于本机临时测试库。容器仅绑定回环地址，不挂载开发数据库的数据卷。

可以直接使用 `.env.test.example`。需要调整时另存为 Git 忽略的 `.env.test`，并把下列命令中的文件名同步替换；已有文件时保留其内容。修改端口需要同时修改端口变量和测试连接地址。

## 可重复执行的步骤

以下步骤会启动测试数据库并在其中创建、删除临时 schema，适用于已授权的本地集成验证。API 与 Worker 在测试进程中调用，不启动常驻服务，不请求真实模型。

1. 安装依赖并静态校验配置：

```bash
uv sync --locked
docker --context colima compose --env-file .env.test.example -f infra/compose/compose.test.yaml config --quiet
```

2. 执行 `colima status`。未运行且准备联调时再执行 `colima start --runtime docker`；已运行时跳过启动。Ubuntu 使用其 Docker Engine，去掉容器命令中的 `--context colima`。

3. 启动独立测试 PostgreSQL 并等待就绪：

```bash
docker --context colima compose --env-file .env.test.example -f infra/compose/compose.test.yaml up -d --wait postgres
```

4. 执行全部测试，包括 PostgreSQL 专用用例：

```bash
uv run --env-file .env.test.example --locked --offline pytest --database-backend=postgresql
```

`--offline` 只限制 uv 的依赖解析与下载；测试仍会连接本机 PostgreSQL。无需手动执行 Alembic，每个数据库用例在自己的 schema 内完成迁移。

排查单项行为时可以指定文件，例如：

```bash
uv run --env-file .env.test.example --locked --offline pytest --database-backend=postgresql tests/test_worker.py tests/test_idempotency.py
```

5. 测试结束后停止专用环境：

```bash
docker --context colima compose --env-file .env.test.example -f infra/compose/compose.test.yaml down
```

停止会丢弃该测试容器的临时数据，不影响 `agentforge-dev` 的持久化卷。此环境不验证容器重建后的数据持久化；开发数据库重启验证按 [开发入门](development.md) 操作。

## 隔离与验收规则

- 必须显式指定 `--database-backend=postgresql` 和 `AGENTFORGE_TEST_DATABASE_URL`，不回退到应用数据库地址。
- 地址必须使用 `postgresql+psycopg`，显式指定主机、用户名和以 `_test` 结尾的专用数据库名。名称检查是误操作防护，不替代专用账号和权限。
- 查询参数只接受连接超时与 TLS 配置，不能通过 `options`、`service`、`dbname` 等覆盖连接目标或 schema。连接超时范围为 1–15 秒，默认 5 秒。
- 每个数据库用例创建唯一的 `af_test_<随机标识>` schema，所有连接都使用它，搜索路径不包含 `public`。测试账号需要在专用测试库中创建 schema 的权限。
- 使用真实连接和事务，不通过单一回滚事务模拟多个 Worker。单条 SQL 最长执行 15 秒，锁等待最长 5 秒。
- 用例成功或失败后只删除它创建的 schema，不清空整个数据库。强制终止可能留下 schema；本机临时环境可通过停止并重建恢复，共享测试实例需确认归属后再清理。
- 验证迁移结构和升级数据保留、并发幂等、身份隔离、并发领取、取消、租约恢复、事件续传与事务一致性。
- 验证连接重建后结果与历史仍可读取，schema 清理不影响其他测试数据。

缺少地址或地址不合规时，pytest 在运行测试前报配置错误，不静默降级到 SQLite。连接失败时检查 Colima、容器健康状态、端口和账号。pytest 不会自动启动容器、切换 Docker 上下文或创建整个数据库。

只有真实 PostgreSQL 模式的结果可以作为集成验证依据；SQLite 测试和 Compose 静态检查不能替代它。当前仍使用模拟模型，不代表真实模型、网络代理或生产部署已验证。

## 验证记录：2026-10-29

- 环境：macOS / Colima，`postgres:17.11-bookworm`，独立 `agentforge-test` 容器。
- SQLite 默认模式：78 项通过，5 项 PostgreSQL 专用用例跳过。
- PostgreSQL 模式：83 项通过；迁移、幂等、租约、事件流及并发行为均已执行。
- Ruff 检查、格式检查和 Compose 静态校验通过。
- 结束时确认临时测试 schema 残留为 0，测试容器与测试网络已清理；原有 Colima 运行状态保留。
- 测试仍有一条既有的 Starlette / httpx 弃用提示，不影响测试通过。真实模型、常驻服务网络联调与生产部署不在本次验收范围内。

### 模型路由功能回归（2026-10-29）

加入模型适配器、工具循环及用量事件后，SQLite 模式为 138 项通过、5 项跳过，PostgreSQL 模式为 143 项通过。模型侧均使用 MockTransport，数据库侧使用真实的独立 PostgreSQL 测试容器；验证后 schema 残留为 0，测试容器和网络已清理。详见 [模型路由说明](model-routing.md)。

### Agent 与会话功能回归（2026-10-29）

- SQLite：167 项通过，5 项 PostgreSQL 专用用例按预期跳过。
- 独立 PostgreSQL 17.11：172 项通过；新增会话多轮上下文、版本固定、并发提交、执行恢复、取消竞争与旧数据迁移验证。
- Ruff 检查、格式检查通过；未增加依赖或改变第三方版本。
- 临时 schema 残留为 0，测试容器及网络已清理；保留原有 Colima 运行状态。
- 模型使用 MockTransport，未调用真实企业路由；未启动常驻 API / Worker，未迁移开发库。
- 保留既有 Starlette / httpx 弃用提示。手动调试见 [Agent 与会话](conversations.md)。

### 工具执行框架回归（2026-10-29）

- SQLite：196 项通过，5 项 PostgreSQL 专用用例跳过。
- PostgreSQL 17.11：201 项通过；新增工具契约、版本与允许列表、作用域隔离、同一尝试的重复调用、取消/过期结果拦截、事务回滚、迁移 0004 和工具事件续传验证。
- Ruff、格式检查通过，无新增依赖或第三方版本变化。
- 临时 schema 残留为 0，测试容器与网络已清理，原有 Colima 保留。
- 模型使用 MockTransport，未调用真实路由或业务系统；未启动常驻 API / Worker，未迁移开发库。
- 仍有一条既有 Starlette / httpx 弃用提示。手动验证见 [版本化工具](tools.md)。

## Skill 安装与 Rakazo 式按需读取验证（2026-10-30）

- SQLite：231 项通过，5 项 PostgreSQL 专用用例跳过。
- PostgreSQL 17.11：236 项通过；覆盖内置与安装目录、并发安装去重、版本摘要冲突、跨身份拒绝、卸载后的快照保留、skill_read 作用域及迁移 0005。
- 模型与 Hub 使用 MockTransport，不访问真实服务。临时 schema 清理后计数为 0，专用测试容器和网络已移除，开发库不变。

# AgentForge 开发协作规范

本文件供在本仓库工作的 AI 编程助手使用，适用于整个仓库。开始修改前阅读相关代码与文档；用户在当前任务中的明确要求优先于本文件。已有授权在其范围内继续有效，无需重复确认。

## 项目定位与实现范围

- AgentForge（智构）是采用 MIT 协议的企业级 Agent 平台，后端统一使用 Python。
- 当前已实现 API、Worker、模拟 Harness、数据库任务队列、任务与事件持久化、提交去重、SSE 续传和租约续期。
- 真实模型、企业认证、长期记忆、Supervisor、Computer 工具沙箱、Web 和桌面端仍属后续建设内容。目录中的职责说明不代表功能已经实现。
- Web 与桌面端保留 React / TypeScript、Electron 的技术方向；不要把“后端使用 Python”扩大为重写所有客户端。
- 当前离线测试使用 SQLite；生产数据库目标是 PostgreSQL。明确区分离线验证、真实数据库验证和生产验证。

## 先阅读的文档

- [README.md](README.md)：项目概览与当前实现范围。
- [docs/architecture.md](docs/architecture.md)：架构基线与模块边界。
- [docs/development.md](docs/development.md)：环境配置、检查命令与手动调试。
- [docs/task-execution.md](docs/task-execution.md)：幂等、事件流、取消与租约协议。
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
| `migrations/` | Alembic 数据库迁移 |
| `tests/` | API、隔离、幂等、事件流、Worker 与迁移的行为验证 |
| `infra/` | 开发部署配置与未来的工具沙箱配置 |

- 当前采用单一 Python 发行包，源目录与导入名由根目录 `pyproject.toml` 显式映射。新增包时同步维护映射，不用临时修改 `sys.path` 解决正式代码的导入问题。
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

- **身份与隔离**：身份范围由服务端解析，任务与事件访问始终校验租户、空间和用户。客户端或模型提供的身份字段不能授予权限。开发令牌不能当作企业认证；缺少配置时不得放行任务接口。
- **事务一致性**：任务状态与对应事件保持事务一致，避免已改变状态却丢失事件。首版任务表兼任持久化队列。
- **提交幂等**：同一身份范围内，相同 `Idempotency-Key` 与请求内容复用同一任务；相同键但不同内容返回冲突。不同身份范围之间不能复用任务。
- **租约与取消**：只有持有有效租约和当前执行标识的 Worker 可以开始新工具调用或写回结果；取消、失效或重新领取后，旧执行不能覆盖任务结果。
- **副作用与恢复**：现有自动重试适用于无外部副作用的模拟工具。接入真实工具时，为外部操作设计独立幂等与结果核对；未知结果不能直接重放。取消状态不代表正在运行的同步函数已被强制终止。
- **事件流**：保留持久化、运行内单调游标与断线续传语义，保持 `Last-Event-ID` 的优先级；等待新事件时不占用数据库连接，不遗漏最终事件。事件流遵守与查询接口相同的身份范围。
- **工具与凭据**：工具参数需要校验，执行范围由代码中的授权规则约束；模型输出、检索材料与记忆不能扩大权限。异常和日志不得泄露令牌、数据库凭据或连接串。

修改上述行为时添加或调整相应的行为测试，覆盖越权、重复提交、取消、租约失效等与改动相关的失败路径。不要仅为复述实现而添加测试。

## Git、文档与交付

- 修改前检查 Git 状态，保留用户已有改动；只暂存与本次任务有关的文件，不重置或覆盖无关内容。
- `.env`、`.env.test`、`.idea/`、`.venv/`、本地数据库及缓存不得提交；`.env.example` 和 `.env.test.example` 只能包含占位值或明确的本地开发、测试示例值。
- 提交与推送按用户授权范围执行；未授权时不自动提交、推送、强制推送或改写历史。新建分支时默认使用 `codex/` 前缀，用户指定名称时遵循其要求。
- 提交前检查暂存差异和 `git diff --cached --check`；区分文件已修改、本地已提交与远程已推送，依据实际结果报告。
- 行为、配置或接口变更同步更新相关文档。说明改动内容、验证结果及尚未验证的范围，默认用中文沟通，保留必要的英文代码名称。
- 保留项目 MIT 许可证及第三方代码原有的来源与许可证声明。

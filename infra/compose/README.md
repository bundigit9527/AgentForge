# 开发环境 PostgreSQL

[compose.dev.yaml](compose.dev.yaml) 仅定义本地开发数据库。API 与 Worker 在本机 Python 环境运行；应用服务和 Computer 工具沙箱的 Compose 配置仍待实现。

macOS 开发使用 Colima 的 Docker 运行时。先运行 `colima status`；若尚未运行，需要联调时手动执行 `colima start --runtime docker`。随后用 `docker --context colima version` 确认 Docker 服务可访问。下列命令均显式选择 `colima` 上下文。

| 配置 | 默认值 |
| --- | --- |
| macOS Docker 环境 | Colima，Docker 上下文为 `colima` |
| 镜像 | `postgres:17.11-bookworm` |
| 本机地址 | `127.0.0.1:55432` |
| 容器内端口 | `5432` |
| 数据库 | `agentforge_dev` |
| 开发账号 | `agentforge` |
| 数据卷 | `agentforge-dev_postgres_data` |

从仓库根目录操作，先复制 `.env.example` 为 `.env`；已有 `.env` 时手动合并所需字段，保留现有配置。此文件已被 Git 忽略。

仅校验配置，不启动容器：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml config --quiet
```

需要联调时再手动启动：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml up -d --wait postgres
uv run --env-file .env --locked alembic upgrade head
```

第一条命令首次可能下载镜像，并初始化专用开发数据库；第二条命令创建应用表。正常停止和重新创建容器保留数据卷，停止命令为：

```bash
docker --context colima compose --env-file .env -f infra/compose/compose.dev.yaml down
```

停止时保留数据卷，不加 `--volumes`。修改 `.env` 中的密码不会更改已有数据卷内的数据库账号密码；需通过数据库账号管理进行修改。修改本机端口或密码时，同步更新 `AGENTFORGE_DATABASE_URL`。

数据卷位于当前 Colima Docker 环境中；切换到其他 Docker 环境不会自动带上该数据卷。这里的 `--context colima` 用于 macOS 开发，Ubuntu 服务器使用其本机 Docker Engine。

当前配置只完成静态校验，尚未拉取镜像、启动容器或运行 PostgreSQL 集成测试。后续服务器部署可接入已有外部 PostgreSQL，详见 [开发入门](../../docs/development.md)。

镜像版本、初始化参数及数据卷规则参考 [PostgreSQL 官方镜像](https://hub.docker.com/_/postgres)。

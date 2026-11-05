ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG CLI_IMAGE=docker:28-cli
FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM ${CLI_IMAGE} AS dockercli
FROM ${PYTHON_IMAGE} AS build
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY apps apps
COPY packages packages
# Application sources change independently of pyproject.toml and uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv uv cache clean agentforge-platform && \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS backend
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations migrations
RUN useradd --uid 1000 --create-home agentforge && install -d -o 1000 -g 1000 -m 0700 /slots /checkpoints
USER 1000:1000
CMD ["uvicorn","agentforge_api.app:create_app","--factory","--host","0.0.0.0","--port","8000"]

FROM backend AS supervisor
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python","-m","agentforge_supervisor.desktop_pool"]

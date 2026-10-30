import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from agentforge_connectors.skill_hub import HttpSkillHub, SkillHubSettings
from agentforge_contracts.conversations import (
    AgentPage,
    AgentResponse,
    AttemptPage,
    CreateAgent,
    CreateThread,
    MessagePage,
    ThreadPage,
    ThreadResponse,
    UpdateAgent,
)
from agentforge_contracts.runs import CreateRun, EventPage, EventResponse, RunResponse
from agentforge_contracts.skills import InstallSkill, RemoteSkillPage, SkillPage
from agentforge_contracts.tools import ToolCallPage, ToolCatalog
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import TERMINAL_STATUSES, RunConflict, RunNotFound, Scope
from agentforge_core.settings import Settings
from agentforge_core.skills import (
    HubInvalidResponse,
    HubUnavailable,
    SkillHub,
    SkillId,
    SkillSnapshot,
)
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import BUILTIN_TOOLS, allowed_tools_from_env
from agentforge_skills.service import SkillService
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import SCHEMA_REVISION
from agentforge_storage.skills import SkillRepository
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from agentforge_api.streaming import stream_events

bearer = HTTPBearer(auto_error=False)


def current_scope(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Scope:
    settings: Settings = request.app.state.settings
    if not settings.dev_token:
        raise HTTPException(503, "Local development authentication is not configured")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), settings.dev_token.encode()
    ):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    return settings.dev_scope


def repository(request: Request) -> RunRepository:
    return request.app.state.repository


ScopeDependency = Annotated[Scope, Depends(current_scope)]
RepositoryDependency = Annotated[RunRepository, Depends(repository)]


def create_app(
    settings: Settings | None = None,
    run_repository: RunRepository | None = None,
    *,
    model_settings: ModelSettings | None = None,
    allowed_tools: tuple[str, ...] | None = None,
    skill_hub: SkillHub | None = None,
) -> FastAPI:
    configuration = settings or Settings.from_env()
    model_configuration = model_settings or ModelSettings.from_env(require_credentials=False)
    tool_allowlist = allowed_tools if allowed_tools is not None else allowed_tools_from_env()
    BUILTIN_TOOLS.validate_allowlist(tool_allowlist)

    def agent_configuration(body, scope):
        try:
            BUILTIN_TOOLS.select(body.tools, tool_allowlist)
        except RuntimeFailure:
            raise HTTPException(422, "Unknown or disallowed tool version") from None
        try:
            return ExecutionSnapshot(
                model=model_configuration.snapshot(),
                instructions=body.instructions,
                tools=body.tools,
                skills=skills.resolve(scope, body.skills),
            )
        except ValidationError:
            raise HTTPException(422, "Invalid skill selection or missing required tools") from None

    owned_engine = (
        None if run_repository is not None else create_database(configuration.database_url)
    )
    store = run_repository if run_repository is not None else RunRepository(owned_engine)

    conversations = ConversationRepository(store.engine, store.clock)
    owned_hub = HttpSkillHub(SkillHubSettings.from_env()) if skill_hub is None else None
    skills = SkillService(SkillRepository(store.engine, store.clock), skill_hub or owned_hub)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if owned_hub is not None:
                owned_hub.close()
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(title="AgentForge", version="0.1.0", lifespan=lifespan)
    app.state.settings = configuration
    app.state.repository = store

    @app.exception_handler(RunNotFound)
    async def missing(_request, _error):
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(RunConflict)
    async def conflict(_request, _error):
        return JSONResponse(status_code=409, content={"detail": "Resource state conflict"})

    @app.exception_handler(HubUnavailable)
    async def hub_unavailable(_request, _error):
        return JSONResponse(
            status_code=503, content={"detail": "Skill Hub is unavailable or not configured"}
        )

    @app.exception_handler(HubInvalidResponse)
    async def hub_invalid(_request, _error):
        return JSONResponse(status_code=502, content={"detail": "Invalid Skill Hub response"})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(repo: RepositoryDependency):
        try:
            with repo.engine.connect() as connection:
                revisions = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
                if revisions != [SCHEMA_REVISION]:
                    raise HTTPException(503, "Database schema upgrade is required")
                connection.execute(text("SELECT run_id FROM runs LIMIT 0"))
                connection.execute(text("SELECT run_id FROM run_events LIMIT 0"))
                connection.execute(text("SELECT execution_config FROM runs LIMIT 0"))
                for table in (
                    "agents",
                    "agent_versions",
                    "threads",
                    "messages",
                    "run_attempts",
                    "tool_calls",
                    "skill_installations",
                ):
                    connection.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database schema is unavailable") from exc
        return {"status": "ok"}

    @app.get("/api/v1/skills", response_model=SkillPage)
    def list_skills(
        scope: ScopeDependency,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        items = skills.list(scope, offset, limit + 1)
        return {
            "items": items[:limit],
            "next_offset": offset + limit if len(items) > limit else None,
        }

    @app.get("/api/v1/skills/hub", response_model=RemoteSkillPage)
    def browse_hub(
        _scope: ScopeDependency,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        q: Annotated[str, Query(max_length=200)] = "",
    ):
        page = skills.hub.catalog(offset, limit, q)
        return {**page.model_dump(), "source_id": skills.hub.source_id}

    @app.get("/api/v1/skills/hub/{skill_id}/versions/{version}")
    def preview_hub(
        skill_id: SkillId, version: Annotated[int, Path(ge=1, le=999999)], _scope: ScopeDependency
    ):
        manifest = skills.hub.manifest(skill_id, version)
        return {
            "source_id": skills.hub.source_id,
            "manifest": manifest,
            "sha256": manifest.digest(),
        }

    @app.post("/api/v1/skills/install", response_model=SkillSnapshot, status_code=201)
    def install_skill(body: InstallSkill, scope: ScopeDependency):
        return skills.install(scope, body)

    @app.delete("/api/v1/skills/installations/{installation_id}", status_code=204)
    def uninstall_skill(installation_id: UUID, scope: ScopeDependency):
        skills.repository.uninstall(scope, str(installation_id))
        return Response(status_code=204)

    @app.get("/api/v1/tools", response_model=ToolCatalog)
    def get_tools(_scope: ScopeDependency):
        return {"items": BUILTIN_TOOLS.catalog(tool_allowlist)}

    @app.get("/api/v1/runs/{run_id}/tool-calls", response_model=ToolCallPage)
    def get_tool_calls(
        run_id: UUID,
        scope: ScopeDependency,
        repo: RepositoryDependency,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ):
        items = repo.list_tool_calls(scope, str(run_id), after, limit)
        return {"items": items, "next_cursor": items[-1]["sequence"] if items else after}

    @app.post("/api/v1/agents", response_model=AgentResponse, status_code=201)
    def create_agent(body: CreateAgent, scope: ScopeDependency):
        config = agent_configuration(body, scope)
        return conversations.create_agent(scope, body.name, config)

    @app.put("/api/v1/agents/{agent_id}", response_model=AgentResponse)
    def update_agent(agent_id: UUID, body: UpdateAgent, scope: ScopeDependency):
        config = agent_configuration(body, scope)
        return conversations.update_agent(
            scope, str(agent_id), body.expected_version, body.name, config
        )

    @app.get("/api/v1/agents", response_model=AgentPage)
    def list_agents(
        scope: ScopeDependency,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        return {"items": conversations.list_agents(scope, offset, limit)}

    @app.get("/api/v1/agents/{agent_id}", response_model=AgentResponse)
    def get_agent(
        agent_id: UUID, scope: ScopeDependency, version: Annotated[int | None, Query(ge=1)] = None
    ):
        return conversations.get_agent(scope, str(agent_id), version)

    @app.post("/api/v1/threads", response_model=ThreadResponse, status_code=201)
    def create_thread(body: CreateThread, scope: ScopeDependency):
        return conversations.create_thread(scope, str(body.agent_id), body.title)

    @app.get("/api/v1/threads", response_model=ThreadPage)
    def list_threads(
        scope: ScopeDependency,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        return {"items": conversations.list_threads(scope, offset, limit)}

    @app.get("/api/v1/threads/{thread_id}", response_model=ThreadResponse)
    def get_thread(thread_id: UUID, scope: ScopeDependency):
        return conversations.get_thread(scope, str(thread_id))

    @app.get("/api/v1/threads/{thread_id}/messages", response_model=MessagePage)
    def get_messages(
        thread_id: UUID,
        scope: ScopeDependency,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        items = conversations.list_messages(scope, str(thread_id), after, limit)
        return {"items": items, "next_cursor": items[-1]["sequence"] if items else after}

    @app.get("/api/v1/runs/{run_id}/attempts", response_model=AttemptPage)
    def get_attempts(run_id: UUID, scope: ScopeDependency, repo: RepositoryDependency):
        return {"items": repo.list_attempts(scope, str(run_id))}

    @app.post("/api/v1/runs", response_model=RunResponse, status_code=201)
    def create_run(
        body: CreateRun,
        scope: ScopeDependency,
        repo: RepositoryDependency,
        idempotency_key: Annotated[
            str | None, Header(pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128)
        ] = None,
    ):
        return RunResponse.from_run(
            repo.create(
                scope,
                body.prompt,
                idempotency_key,
                thread_id=str(body.thread_id) if body.thread_id else None,
                execution_config=ExecutionSnapshot(
                    model=model_configuration.snapshot(),
                    tools=("example_report@1",) if "example_report@1" in tool_allowlist else (),
                ),
            )
        )

    @app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: UUID, scope: ScopeDependency, repo: RepositoryDependency):
        return RunResponse.from_run(repo.get(scope, str(run_id)))

    @app.post("/api/v1/runs/{run_id}/cancel", response_model=RunResponse)
    def cancel_run(run_id: UUID, scope: ScopeDependency, repo: RepositoryDependency):
        return RunResponse.from_run(repo.cancel(scope, str(run_id)))

    @app.get("/api/v1/runs/{run_id}/events", response_model=EventPage)
    def get_events(
        run_id: UUID,
        scope: ScopeDependency,
        repo: RepositoryDependency,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        items = repo.list_events(scope, str(run_id), after, limit)
        return EventPage(
            items=[EventResponse.from_event(item) for item in items],
            next_cursor=items[-1].sequence if items else after,
        )

    @app.get("/api/v1/runs/{run_id}/events/stream", response_class=StreamingResponse)
    async def get_event_stream(
        run_id: UUID,
        request: Request,
        scope: ScopeDependency,
        repo: RepositoryDependency,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        last_event_id: Annotated[
            str | None, Header(pattern=r"^[0-9]{1,19}$", max_length=19)
        ] = None,
    ):
        cursor = int(last_event_id) if last_event_id is not None else after
        if cursor > 2**63 - 1:
            raise HTTPException(422, "Event cursor is out of range")
        # Authorization and cursor validation must finish before response headers are sent.
        run = await run_in_threadpool(repo.get, scope, str(run_id))
        if run.status in TERMINAL_STATUSES:
            remaining = await run_in_threadpool(repo.list_events, scope, str(run_id), cursor, 1)
            if not remaining:
                return Response(status_code=204)
        return StreamingResponse(
            stream_events(request, repo, scope, str(run_id), cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app

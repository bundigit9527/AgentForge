import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from agentforge_contracts.runs import CreateRun, EventPage, EventResponse, RunResponse
from agentforge_core.models import TERMINAL_STATUSES, RunConflict, RunNotFound, Scope
from agentforge_core.settings import Settings
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import SCHEMA_REVISION
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
    settings: Settings | None = None, run_repository: RunRepository | None = None
) -> FastAPI:
    configuration = settings or Settings.from_env()
    owned_engine = (
        None if run_repository is not None else create_database(configuration.database_url)
    )
    store = run_repository if run_repository is not None else RunRepository(owned_engine)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(title="AgentForge", version="0.1.0", lifespan=lifespan)
    app.state.settings = configuration
    app.state.repository = store

    @app.exception_handler(RunNotFound)
    async def missing(_request, _error):
        return JSONResponse(status_code=404, content={"detail": "Run not found"})

    @app.exception_handler(RunConflict)
    async def conflict(_request, _error):
        return JSONResponse(status_code=409, content={"detail": "Run state conflict"})

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
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database schema is unavailable") from exc
        return {"status": "ok"}

    @app.post("/api/v1/runs", response_model=RunResponse, status_code=201)
    def create_run(
        body: CreateRun,
        scope: ScopeDependency,
        repo: RepositoryDependency,
        idempotency_key: Annotated[
            str | None, Header(pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128)
        ] = None,
    ):
        return RunResponse.from_run(repo.create(scope, body.prompt, idempotency_key))

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

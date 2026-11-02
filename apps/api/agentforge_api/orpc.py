"""oRPC v1 JSON / HTTP compatibility for the pinned Rakazo core chat contract."""

import asyncio
import json
import logging
import time

from agentforge_contracts.rakazo import (
    Bootstrap,
    CreateBot,
    Empty,
    History,
    Send,
    Subscribe,
    Target,
    UpdateBot,
)
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.identity import (
    AuthenticationFailed,
    AuthenticationUnavailable,
    PermissionDenied,
)
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_runtime.errors import RuntimeFailure
from agentforge_storage.rakazo import RakazoRepository
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message

    def envelope(self):
        return {
            "json": {
                "defined": False,
                "code": self.code,
                "status": self.status,
                "message": self.message,
            }
        }


def failure(exc):
    if isinstance(exc, RpcError):
        return exc
    for cls, status, code, message in (
        (AuthenticationFailed, 401, "UNAUTHORIZED", "Invalid or expired credentials"),
        (AuthenticationUnavailable, 503, "SERVICE_UNAVAILABLE", "Authentication unavailable"),
        (PermissionDenied, 403, "FORBIDDEN", "Insufficient permission"),
        (RunNotFound, 404, "NOT_FOUND", "Resource not found"),
        (RunConflict, 409, "CONFLICT", "Resource state conflict"),
        (ValidationError, 422, "UNPROCESSABLE_CONTENT", "Invalid procedure input"),
        (SQLAlchemyError, 503, "SERVICE_UNAVAILABLE", "Storage unavailable"),
        (RuntimeFailure, 422, "UNPROCESSABLE_CONTENT", "Invalid execution configuration"),
    ):
        if isinstance(exc, cls):
            return RpcError(status, code, message)
    logger.error("Unexpected RPC failure", extra={"error_type": type(exc).__name__})
    return RpcError(500, "INTERNAL_SERVER_ERROR", "Internal server error")


def sse(value, *, event="message", identifier=None):
    prefix = f"id: {identifier}\n" if identifier is not None else ""
    return (
        prefix
        + f"event: {event}\ndata: "
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


async def subscribe_events(
    request, service, scope, bot_id, cursor, authorize, *, poll_interval=0.25, heartbeat_interval=15
):
    last_output = time.monotonic()
    yield ": connected\n\n"
    while not await request.is_disconnected():
        try:
            if await run_in_threadpool(authorize) != scope:
                raise AuthenticationFailed()
            batch = await run_in_threadpool(service.events, scope, bot_id, cursor)
            for event in batch:
                if await request.is_disconnected():
                    return
                if await run_in_threadpool(authorize) != scope:
                    raise AuthenticationFailed()
                await run_in_threadpool(service.head, scope, bot_id)
                yield sse({"json": event}, identifier=event["seq"])
                cursor = event["seq"]
                last_output = time.monotonic()
        except Exception as exc:
            yield sse(failure(exc).envelope(), event="error")
            return
        if len(batch) == 100:
            continue
        if time.monotonic() - last_output >= heartbeat_interval:
            yield ": keep-alive\n\n"
            last_output = time.monotonic()
        await asyncio.sleep(poll_interval)


async def read_input(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise RpcError(415, "UNSUPPORTED_MEDIA_TYPE", "Expected application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 128000:
            raise RpcError(413, "PAYLOAD_TOO_LARGE", "Request too large")
    try:
        obj = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise RpcError(400, "BAD_REQUEST", "Invalid JSON") from None
    if (
        not isinstance(obj, dict)
        or set(obj) - {"json", "meta"}
        or obj.get("meta") not in (None, [])
    ):
        raise RpcError(400, "BAD_REQUEST", "Only JSON inputs are supported")
    value = obj.get("json", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RpcError(422, "UNPROCESSABLE_CONTENT", "Expected an object")
    return value


def mount_orpc(app, store, model_configuration, tool_allowlist, browser, authenticate):
    service = RakazoRepository(store)
    app.state.rakazo = service

    def me(scope):
        user, _ = browser.profile(scope)
        model = model_configuration.snapshot()
        return {
            "ticketBoardEnabled": False,
            "userId": scope.user_id,
            "email": user["email"],
            "name": user["name"],
            "spaceId": scope.space_id,
            "isDeploymentOwner": False,
            "needsModel": False,
            "defaultProvider": model.provider,
            "defaultModel": model.model,
            "hostCredentialProvider": None,
            "hostCredentialSource": None,
            "computerHost": None,
            "canChooseHostComputer": False,
            "sandboxProvider": "unavailable",
            "avatarStyle": "robot",
            "billingEnabled": False,
        }

    def spaces(scope, bots):
        _, name = browser.profile(scope)
        return [
            {
                "id": scope.space_id,
                "name": name,
                "isDefault": True,
                "hasContent": bool(bots),
                "canRename": False,
                "canDelete": False,
                "bots": bots,
                "groups": [],
                "externalConversations": [],
                "botSections": [],
            }
        ]

    def dispatch(scope, procedure, data):
        if procedure == "health":
            Empty.model_validate(data)
            return {"ok": True, "version": "agentforge-0.1.0"}
        if procedure == "me":
            Empty.model_validate(data)
            return me(scope)
        if procedure in {"bootstrap", "spaces/list", "bots/list"}:
            body = (
                Bootstrap.model_validate(data)
                if procedure == "bootstrap"
                else Empty.model_validate(data)
            )
            bots = service.bots(scope)
            if procedure == "bots/list":
                return bots
            navigation = spaces(scope, bots)
            if procedure == "spaces/list":
                return {
                    "current": {
                        "id": scope.space_id,
                        "name": navigation[0]["name"],
                        "bots": bots,
                        "groups": [],
                        "externalConversations": [],
                        "botSections": [],
                    },
                    "spaces": navigation,
                }
            selected = str(body.botId) if body.botId else (bots[0]["id"] if bots else None)
            return {
                "me": me(scope),
                "bots": bots,
                "groups": [],
                "botSections": [],
                "archivedBots": [],
                "archivedGroups": [],
                "routines": [],
                "spaces": navigation,
                "thread": service.snapshot(scope, selected) if selected else None,
            }
        if procedure == "bots/create":
            body = CreateBot.model_validate(data)
            config = ExecutionSnapshot(
                authorization_mode=store.authorization.mode,
                model=model_configuration.snapshot(),
                instructions=body.instructions,
                tools=("example_report@1",) if "example_report@1" in tool_allowlist else (),
            )
            return service.create_bot(scope, body.model_dump(mode="json"), config)
        if procedure == "bots/update":
            body = UpdateBot.model_validate(data)
            if any(v is None for v in body.model_dump(exclude_unset=True).values()):
                raise RpcError(
                    422, "UNPROCESSABLE_CONTENT", "Null profile fields are not supported"
                )
            return service.update_bot(scope, body.model_dump(mode="json", exclude_unset=True))
        targets = {
            "bots/get": service.bot,
            "threads/get": service.snapshot,
            "threads/head": service.head,
            "threads/stop": service.stop,
        }
        if procedure in targets:
            return targets[procedure](scope, str(Target.model_validate(data).botId))
        if procedure == "threads/messages":
            body = History.model_validate(data)
            return service.history(scope, str(body.botId), body.before)
        if procedure == "threads/send":
            return service.send(scope, Send.model_validate(data).model_dump(mode="json"))
        # Empty collections accurately describe capabilities not present in this backend.
        if procedure in {
            "groups/list",
            "groups/listArchived",
            "bots/listArchived",
            "botSections/list",
            "routines/list",
            "models/list",
            "models/credentials",
        }:
            Empty.model_validate(data)
            return []
        raise RpcError(
            501, "NOT_IMPLEMENTED", "Procedure is not supported by this AgentForge version"
        )

    @app.api_route("/rpc/{procedure:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def rpc(procedure: str, request: Request):
        try:
            # Auth errors must also use the oRPC envelope, not FastAPI's REST error shape.
            scope = await run_in_threadpool(authenticate, request)
            if request.method != "POST":
                raise RpcError(405, "METHOD_NOT_SUPPORTED", "Use POST for RPC procedures")
            data = await read_input(request)
            if procedure == "threads/subscribe":
                body = Subscribe.model_validate(data)
                head = await run_in_threadpool(service.head, scope, str(body.botId))
                cursor = body.cursor
                if "last-event-id" in request.headers:
                    raw = request.headers["last-event-id"]
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 16:
                        raise RpcError(400, "BAD_REQUEST", "Invalid event cursor")
                    cursor = int(raw)
                if cursor > head["cursor"]:
                    raise RpcError(409, "CONFLICT", "Event cursor is ahead of this conversation")
                return StreamingResponse(
                    subscribe_events(
                        request,
                        service,
                        scope,
                        str(body.botId),
                        cursor,
                        lambda: authenticate(request),
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                )
            result = await run_in_threadpool(dispatch, scope, procedure, data)
            return JSONResponse({"json": result}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            error = failure(exc)
            return JSONResponse(
                error.envelope(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )

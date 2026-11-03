"""oRPC v1 JSON / HTTP compatibility for the pinned Rakazo core chat contract."""

import asyncio
import json
import logging
import time

from agentforge_contracts.rakazo import (
    AnswerApproval,
    ArtifactFamily,
    ArtifactId,
    Bootstrap,
    BotArtifact,
    ConversationList,
    CreateBot,
    CreateConversation,
    CreateSkill,
    Empty,
    History,
    ListSpaceArtifacts,
    ReleaseDesktop,
    RemoveSkill,
    SelectConversation,
    Send,
    SetDefaultModel,
    SkillTarget,
    Subscribe,
    Target,
    UpdateBot,
    UpdateSkill,
)
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.identity import (
    AuthenticationFailed,
    AuthenticationUnavailable,
    PermissionDenied,
)
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.skills import SkillDocumentError
from agentforge_runtime.errors import RuntimeFailure
from agentforge_storage.approvals import ApprovalRepository
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_storage.rakazo import RakazoRepository
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from agentforge_api.desktop import mount_desktop
from agentforge_api.rakazo_models import ModelRpcAdapter
from agentforge_api.rakazo_skills import SkillRpcAdapter

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
    # Only these fixed lifecycle messages are safe to expose; all other conflicts
    # retain the generic error and never leak resource identifiers or storage details.
    if isinstance(exc, RunConflict) and str(exc) in {
        "The selected conversation changed; refresh and try again",
        "Stop current work before changing or clearing the conversation",
        "Conversation request was already used",
        "Conversation request was already used with another title",
    }:
        return RpcError(409, "CONFLICT", str(exc))
    for cls, status, code, message in (
        (AuthenticationFailed, 401, "UNAUTHORIZED", "Invalid or expired credentials"),
        (AuthenticationUnavailable, 503, "SERVICE_UNAVAILABLE", "Authentication unavailable"),
        (PermissionDenied, 403, "FORBIDDEN", "Insufficient permission"),
        (RunNotFound, 404, "NOT_FOUND", "Resource not found"),
        (RunConflict, 409, "CONFLICT", "Resource state conflict"),
        (
            SkillDocumentError,
            422,
            "UNPROCESSABLE_CONTENT",
            "Invalid SKILL.md or skill size limit exceeded",
        ),
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
    request,
    service,
    scope,
    bot_id,
    cursor,
    authorize,
    *,
    poll_interval=0.25,
    heartbeat_interval=15,
    expected_thread_id=None,
):
    if expected_thread_id is None:
        expected_thread_id = (await run_in_threadpool(service.head, scope, bot_id))["threadId"]
    last_output = time.monotonic()
    yield ": connected\n\n"
    while not await request.is_disconnected():
        try:
            if await run_in_threadpool(authorize) != scope:
                raise AuthenticationFailed()
            batch = await run_in_threadpool(
                service.events, scope, bot_id, cursor, expected_thread_id=expected_thread_id
            )
            for event in batch:
                if await request.is_disconnected():
                    return
                if await run_in_threadpool(authorize) != scope:
                    raise AuthenticationFailed()
                await run_in_threadpool(
                    service.head, scope, bot_id, expected_thread_id=expected_thread_id
                )
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


def mount_orpc(app, store, model_configuration, tool_allowlist, browser, authenticate, skills):
    from agentforge_api.browser_auth import COOKIE

    desktop = mount_desktop(app, store, browser)
    artifacts = ArtifactRepository(store)
    service = RakazoRepository(store)
    model_adapter = ModelRpcAdapter(store, model_configuration)
    app.state.rakazo = service
    skill_adapter = SkillRpcAdapter(skills, tool_allowlist)

    def me(scope):
        user, _ = browser.profile(scope)
        model = model_adapter.preferences.default(scope).snapshot()
        return {
            "ticketBoardEnabled": True,
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
            "sandboxProvider": "docker"
            if desktop and desktop.assigned(desktop.assignment(), scope)
            else "unavailable",
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

    def dispatch(scope, procedure, data, expected_thread_id=None, cookie=None):
        def snapshot(bot):
            snap = service.snapshot(scope, bot, expected_thread_id=expected_thread_id)
            return desktop.snapshot(scope, bot, cookie, snap) if desktop else snap

        if desktop and procedure.startswith("computer/"):
            action = procedure.removeprefix("computer/")
            if action == "updates":
                Empty.model_validate(data)
                return []
            if action in {"status", "boot", "release", "takeover", "heartbeat", "screenUrl"}:
                model = ReleaseDesktop if action == "release" else Target
                return desktop.handle(action, scope, str(model.model_validate(data).botId), cookie)
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
                "thread": snapshot(selected) if selected else None,
            }
        if procedure in {"models/list", "models/credentials"}:
            Empty.model_validate(data)
            return (
                model_adapter.catalog()
                if procedure == "models/list"
                else model_adapter.credentials(scope)
            )
        if procedure == "models/setDefault":
            body = SetDefaultModel.model_validate(data)
            try:
                return model_adapter.preferences.set_default(scope, body.provider, body.modelId)
            except ValueError:
                raise RpcError(
                    422, "UNPROCESSABLE_CONTENT", "Choose a server-configured model"
                ) from None
        if procedure == "agentSkills/list":
            Empty.model_validate(data)
            return skill_adapter.list(scope)
        if procedure == "agentSkills/get":
            return skill_adapter.get(scope, SkillTarget.model_validate(data))
        skill_writes = {
            "agentSkills/create": (CreateSkill, skill_adapter.create),
            "agentSkills/update": (UpdateSkill, skill_adapter.update),
            "agentSkills/remove": (RemoveSkill, skill_adapter.remove),
        }
        if procedure in skill_writes:
            model, action = skill_writes[procedure]
            return action(scope, model.model_validate(data))
        if procedure == "bots/create":
            body = CreateBot.model_validate(data)
            config = ExecutionSnapshot(
                authorization_mode=store.authorization.mode,
                model=model_adapter.preferences.default(scope).snapshot(),
                instructions=body.instructions,
                tools=("example_report@1",) if "example_report@1" in tool_allowlist else (),
            )
            return service.create_bot(scope, body.model_dump(mode="json"), config)
        if procedure == "bots/update":
            body = UpdateBot.model_validate(data)
            try:
                return service.update_bot(
                    scope, body.model_dump(mode="json", exclude_unset=True), model_configuration
                )
            except ValueError:
                raise RpcError(
                    422, "UNPROCESSABLE_CONTENT", "Model selection is configured by the server"
                ) from None
        if procedure == "agentforge/conversations/list":
            body = ConversationList.model_validate(data)
            return service.conversations_page(scope, str(body.botId), body.offset, body.limit)
        if procedure in {"agentforge/conversations/create", "agentforge/conversations/select"}:
            model = CreateConversation if procedure.endswith("/create") else SelectConversation
            return service.change_conversation(
                scope,
                model.model_validate(data).model_dump(mode="json"),
                create=procedure.endswith("/create"),
            )
        if procedure == "artifacts/list":
            return artifacts.listing(scope, str(Target.model_validate(data).botId))
        if procedure == "artifacts/listSpace":
            page = ListSpaceArtifacts.model_validate(data)
            return artifacts.listing(
                scope,
                str(page.botId) if page.botId else None,
                limit=page.limit,
                cursor=str(page.cursor) if page.cursor else None,
            )
        if procedure == "artifacts/get":
            target = BotArtifact.model_validate(data)
            return artifacts.get(scope, str(target.artifactId), bot_id=str(target.botId))
        if procedure == "artifacts/getById":
            return artifacts.get(scope, str(ArtifactId.model_validate(data).artifactId))
        if procedure == "artifacts/listVersions":
            item = artifacts.get(
                scope, str(ArtifactFamily.model_validate(data).familyId), content=False
            )
            return [{k: item[k] for k in ("id", "version", "name", "createdAt")}]
        if procedure == "threads/answer":
            body = AnswerApproval.model_validate(data)
            # Native callers target a Bot; resolve the caller's selected Thread first.
            head = service.head(scope, str(body.botId), expected_thread_id=expected_thread_id)
            if body.threadId is not None and str(body.threadId) != head["threadId"]:
                raise RunConflict("Conversation has changed")
            return ApprovalRepository(store).answer(
                scope,
                str(body.runId),
                str(body.messageId),
                body.answer,
                bot_id=str(body.botId),
                thread_id=head["threadId"],
            )
        if procedure == "threads/clear":
            return service.clear(
                scope, str(Target.model_validate(data).botId), expected_thread_id=expected_thread_id
            )
        targets = {
            "bots/get": service.bot,
            "threads/get": service.snapshot,
            "threads/head": service.head,
            "threads/stop": service.stop,
        }
        if procedure in targets:
            target = str(Target.model_validate(data).botId)
            if procedure == "bots/get":
                return service.bot(scope, target)
            if procedure == "threads/get":
                return snapshot(target)
            return targets[procedure](scope, target, expected_thread_id=expected_thread_id)
        if procedure == "threads/messages":
            body = History.model_validate(data)
            return service.history(
                scope, str(body.botId), body.before, expected_thread_id=expected_thread_id
            )
        if procedure == "threads/send":
            body = Send.model_validate(data)
            return service.send(
                scope,
                body.model_dump(mode="json"),
                expected_thread_id=expected_thread_id,
                resolve_config=lambda connection, config: skill_adapter.prepare_run(
                    scope, body.text, config, connection=connection
                ),
            )
        # Empty collections accurately describe capabilities not present in this backend.
        if procedure in {
            "groups/list",
            "groups/listArchived",
            "bots/listArchived",
            "botSections/list",
            "routines/list",
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
            expected_thread_id = request.headers.get("x-agentforge-thread-id")
            if expected_thread_id:
                from uuid import UUID

                try:
                    expected_thread_id = str(UUID(expected_thread_id))
                except ValueError:
                    raise RpcError(
                        422, "UNPROCESSABLE_CONTENT", "Invalid conversation precondition"
                    ) from None
            data = await read_input(request)
            if procedure == "threads/subscribe":
                body = Subscribe.model_validate(data)
                head = await run_in_threadpool(
                    service.head, scope, str(body.botId), expected_thread_id=expected_thread_id
                )
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
                        expected_thread_id=head["threadId"],
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                )
            result = await run_in_threadpool(
                dispatch, scope, procedure, data, expected_thread_id, request.cookies.get(COOKIE)
            )
            return JSONResponse({"json": result}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            error = failure(exc)
            return JSONResponse(
                error.envelope(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )

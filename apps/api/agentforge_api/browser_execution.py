"""Trusted Worker browser gateway: resolve Run identity, reserve its desktop, recheck each step."""

import asyncio
import hmac
import os
import secrets
import threading
import time
from uuid import UUID

from agentforge_connectors.browser import BrowserPolicy, service_configuration
from agentforge_core.browser import READING_ACTIONS, BrowserInput, browser_result
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool


class Execute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runId: UUID
    attemptId: UUID
    operation: BrowserInput


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operationId: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def mount_browser_execution(app, store, directory):
    policy = BrowserPolicy.from_env()
    token = os.environ.get("AGENTFORGE_BROWSER_TOKEN", "")
    if policy.grants:
        service_configuration()
        if not directory or not directory.pool:
            raise ValueError("Visible browser requires the scoped desktop pool")
    if token and (len(token) < 32 or token == os.environ.get("SCREEN_PROXY_SECRET")):
        raise ValueError("Use a dedicated browser service token")
    operations = {}
    lock = threading.Lock()

    def error(code, status=409):
        return JSONResponse(
            {"error": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    def valid(claim):
        if not claim or not claim.run.agent_id or not claim.run.execution_config:
            return False
        origins = claim.run.execution_config.browser_origins
        current = BrowserPolicy.from_env().origins(claim.run.scope)
        return bool(origins) and ("*" in current or set(origins) <= set(current))

    async def body(request, model):
        if not token:
            return error("browser_unavailable", 503)
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
        ):
            return error("browser_not_allowed", 403)
        data = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > 8192:
                        raise ValueError()
            return model.model_validate_json(data)
        except (ValueError, TimeoutError):
            return error("browser_not_allowed", 400)

    @app.post("/api/internal/browser/check", include_in_schema=False)
    async def check(request: Request):
        parsed = await body(request, Check)
        if isinstance(parsed, JSONResponse):
            return parsed
        with lock:
            entry = operations.get(parsed.operationId)
        if not entry:
            return error("browser_not_allowed", 403)
        claim, slot, generation, expiry = entry
        current = await run_in_threadpool(store.active_claim, claim.run.run_id, claim.attempt_id)
        try:
            permitted = valid(current)
        except ValueError:
            permitted = False
        if (
            not permitted
            or time.monotonic() > expiry
            or slot.generation != generation
            or slot.reader_id != parsed.operationId
            or slot.controller
        ):
            return error("browser_not_allowed", 403)
        return {"ok": True}

    def execute(parsed):
        claim = store.active_claim(str(parsed.runId), str(parsed.attemptId))
        if not valid(claim):
            return error("browser_not_allowed", 403)
        if (
            parsed.operation.action in READING_ACTIONS
            and not claim.run.execution_config.browser_reading
        ):
            return error("browser_not_allowed", 403)
        if not directory or not directory.pool:
            return error("browser_unavailable", 503)
        slot = directory.select(claim.run.scope, claim.run.agent_id)
        if not slot:
            return error("browser_not_allowed", 403)
        if not slot.lock.acquire(blocking=False):
            return error("browser_busy")
        operation = secrets.token_urlsafe(32)
        try:
            slot.authorize(claim.run.scope, claim.run.agent_id, True)
            entry = slot.assignment()
            slot.sync(entry)
            if slot.controller or slot.reader_id:
                return error("browser_busy")
            if not entry:
                entry = slot.lifecycle("boot")
                slot.sync(entry)
            if not valid(store.active_claim(claim.run.run_id, claim.attempt_id)):
                return error("browser_not_allowed", 403)
            expiry = time.monotonic() + 32
            slot.reader_id = operation
            slot.reader_end = expiry
            slot.browser_entry = entry
            slot.activity = time.monotonic()
            with lock:
                operations[operation] = (claim, slot, entry["generation"], expiry)
        except (RunConflict, RunNotFound, PermissionDenied):
            return error("browser_unavailable")
        finally:
            slot.lock.release()
        try:
            reply = slot.exchange(
                "browser",
                entry["generation"],
                {
                    "runId": claim.run.run_id,
                    "attemptId": claim.attempt_id,
                    "operationId": operation,
                    "origins": list(claim.run.execution_config.browser_origins),
                    "operation": parsed.operation.model_dump(),
                },
            )
            if not valid(store.active_claim(claim.run.run_id, claim.attempt_id)):
                return error("browser_not_allowed", 403)
            if reply.get("error") in {
                "browser_not_allowed",
                "browser_page_changed",
                "browser_failed",
            }:
                return error(reply["error"])
            if "result" not in reply:
                return error("browser_unavailable")
            return browser_result(parsed.operation.action, reply["result"])
        except (RunConflict, RunNotFound, PermissionDenied, ValueError):
            return error("browser_unavailable")
        finally:
            with lock:
                operations.pop(operation, None)
            with slot.lock:
                if slot.reader_id == operation:
                    slot.reader_id = None
                slot.browser_entry = None
                slot.activity = time.monotonic()

    @app.post("/api/internal/browser/execute", include_in_schema=False)
    async def run_browser(request: Request):
        parsed = await body(request, Execute)
        if isinstance(parsed, JSONResponse):
            return parsed
        try:
            return await run_in_threadpool(execute, parsed)
        except (ValueError, OSError):
            return error("browser_unavailable", 503)

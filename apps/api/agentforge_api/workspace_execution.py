"""Existing desktop control plane, with scoped file reads and approved copy operations."""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Literal
from uuid import UUID

from agentforge_connectors.browser import service_configuration
from agentforge_connectors.python_execution import PythonPolicy, PythonSettings
from agentforge_connectors.workspace import workspace_policy
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.python_execution import output_name
from agentforge_core.workspace import argument_model
from agentforge_storage.file_continuity import allowed_file, input_filename
from agentforge_storage.workspace import dispatched_tool
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runId: UUID
    attemptId: UUID
    tool: Literal["workspace_save", "attach_file", "list_files", "read_file", "write_file"]
    arguments: dict


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operationId: str


def mount_workspace_execution(app, store, directory):
    token = os.environ.get("AGENTFORGE_BROWSER_TOKEN", "")
    if workspace_policy().grants:
        service_configuration()
        if not directory or not directory.pool:
            raise ValueError("Workspace tools require the scoped desktop pool")
    operations, lock = {}, threading.Lock()

    def error(code, status=409):
        return JSONResponse(
            {"error": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    async def parse(request, model):
        if not token or not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
        ):
            return error("workspace_not_allowed", 403)
        try:
            data = bytearray()
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > 100000:
                        raise ValueError()
            return model.model_validate_json(data)
        except (ValueError, TimeoutError):
            return error("workspace_invalid", 400)

    def valid(claim, tool, arguments):
        if not claim or not claim.run.agent_id or not claim.run.execution_config:
            return False
        config = claim.run.execution_config
        return bool(
            config.workspace_enabled
            and config.workspace_mode
            and workspace_policy().permits(claim.run.scope)
            and PythonPolicy.from_env().permits(claim.run.scope)
            and PythonSettings.from_env().image == config.python_image
            and (
                tool not in {"workspace_save", "write_file"}
                or arguments["computer_mode"] == config.workspace_mode
            )
            and dispatched_tool(store, claim, tool, arguments)
        )

    @app.post("/api/internal/workspace/check", include_in_schema=False)
    async def check(request: Request):
        body = await parse(request, CheckBody)
        if isinstance(body, JSONResponse):
            return body
        with lock:
            saved = operations.get(body.operationId)
        if not saved:
            return error("workspace_not_allowed", 403)
        claim, slot, generation, tool, arguments, expiry = saved
        current = await run_in_threadpool(store.active_claim, claim.run.run_id, claim.attempt_id)
        try:
            permitted = valid(current, tool, arguments)
        except ValueError:
            permitted = False
        if (
            not permitted
            or time.monotonic() > expiry
            or slot.generation != generation
            or slot.reader_id != body.operationId
            or slot.controller
        ):
            return error("workspace_not_allowed", 403)
        return {"ok": True}

    def execute(body):
        claim = store.active_claim(str(body.runId), str(body.attemptId))
        if not claim or not claim.run.execution_config:
            return error("workspace_not_allowed", 403)
        arguments = (
            argument_model(
                body.tool,
                claim.run.execution_config.workspace_file_tools,
                claim.run.execution_config.workspace_file_alignment,
            )
            .model_validate(body.arguments)
            .model_dump()
        )
        if not valid(claim, body.tool, arguments):
            return error("workspace_not_allowed", 403)
        if not directory or not directory.pool:
            return error("workspace_unavailable", 503)
        slot = directory.select(claim.run.scope, claim.run.agent_id)
        if not slot or not slot.lock.acquire(blocking=False):
            return error("workspace_busy")
        operation = secrets.token_urlsafe(32)
        try:
            slot.authorize(claim.run.scope, claim.run.agent_id, True)
            if ("team" if slot.team else "dedicated") != claim.run.execution_config.workspace_mode:
                return error("workspace_changed")
            entry = slot.assignment()
            slot.sync(entry)
            if slot.controller or slot.reader_id:
                return error("workspace_busy")
            if not entry:
                entry = slot.lifecycle("boot")
                slot.sync(entry)
            if not valid(
                store.active_claim(claim.run.run_id, claim.attempt_id), body.tool, arguments
            ):
                return error("workspace_not_allowed", 403)
            expiry = time.monotonic() + 170
            slot.reader_id, slot.reader_end, slot.browser_entry = operation, expiry, entry
            with lock:
                operations[operation] = (
                    claim,
                    slot,
                    entry["generation"],
                    body.tool,
                    arguments,
                    expiry,
                )
        finally:
            slot.lock.release()
        try:
            if body.tool == "workspace_save":
                with store.engine.connect() as conn:
                    row = allowed_file(store, conn, claim, arguments["artifact_id"])
                name = input_filename(row)
                action = "saveArtifact"
                request = dict(
                    artifactId=row["artifact_id"],
                    name=name,
                    path=f"artifacts/{row['artifact_id']}/{name}",
                    contentBase64=base64.b64encode(row["data"]).decode(),
                    sha256=hashlib.sha256(row["data"]).hexdigest(),
                )
            elif body.tool in {"read_file", "write_file"}:
                action = "readText" if body.tool == "read_file" else "writeText"
                if (
                    body.tool == "write_file"
                    and claim.run.execution_config.workspace_file_alignment
                ):
                    action = "writeTextV2"
                request = {k: v for k, v in arguments.items() if k != "computer_mode"}
            else:
                action = "downloadFile" if body.tool == "attach_file" else "files"
                request = {"path": arguments["path"]}
            if slot.team:
                request["workspaceBot"] = claim.run.agent_id
            reply = slot.exchange(
                "workspace",
                entry["generation"],
                dict(
                    operationId=operation,
                    action=action,
                    request=request,
                ),
            )
            if not valid(
                store.active_claim(claim.run.run_id, claim.attempt_id), body.tool, arguments
            ):
                return error("workspace_not_allowed", 403)
            if "result" not in reply:
                code = reply.get("error")
                if body.tool in {"read_file", "write_file"} and code in {
                    "workspace_file_changed",
                    "workspace_file_unsupported",
                    "workspace_file_missing",
                }:
                    return error(code)
                return error("workspace_failed")
            result = reply["result"]
            if body.tool in {"workspace_save", "write_file"}:
                return {**result, "computer_mode": claim.run.execution_config.workspace_mode}
            if body.tool == "read_file":
                return result
            if body.tool == "attach_file":
                return {
                    "name": output_name(
                        arguments["path"].rsplit("/", 1)[-1],
                        workspace=claim.run.execution_config.workspace_file_alignment,
                    ),
                    "data": result["contentBase64"],
                }
            # Fit metadata in the shared tool-result budget; file names remain untrusted data.
            entries, size = [], 0
            for item in result:
                import json

                size += len(json.dumps(item, ensure_ascii=False).encode())
                if len(entries) >= 50 or size > 12000:
                    break
                entries.append(item)
            return {"entries": entries, "truncated": len(entries) != len(result)}
        finally:
            with lock:
                operations.pop(operation, None)
            with slot.lock:
                if slot.reader_id == operation:
                    slot.reader_id = None
                slot.browser_entry = None
                slot.activity = time.monotonic()

    @app.post("/api/internal/workspace/execute", include_in_schema=False)
    async def run(request: Request):
        body = await parse(request, RequestBody)
        if isinstance(body, JSONResponse):
            return body
        try:
            return await run_in_threadpool(execute, body)
        except (ValueError, OSError, RunConflict, RunNotFound, PermissionDenied):
            return error("workspace_unavailable")

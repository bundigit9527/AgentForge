"""Explicit workspace grants; existing authenticated desktop control-plane transport."""

import os
from pathlib import Path

import httpx
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_core.workspace import WORKSPACE_TOOLS
from agentforge_runtime.errors import RuntimeFailure

from agentforge_connectors.browser import service_configuration
from agentforge_connectors.python_execution import PythonPolicy


def workspace_policy():
    path = os.environ.get("AGENTFORGE_WORKSPACE_POLICY_FILE", "")
    if not path:
        return PythonPolicy()
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(128001)
        if len(data) > 128000:
            raise ValueError()
        return PythonPolicy.model_validate_json(data)
    except (ValueError, OSError):
        raise ValueError("Invalid workspace policy") from None


def prepare_workspace(scope, config):
    enabled = (
        config.python_continuity
        and workspace_policy().permits(scope)
        and len(set(config.tool_references) - set(WORKSPACE_TOOLS)) + len(WORKSPACE_TOOLS) <= 16
    )
    approvals = tuple(t for t in config.approval_tools if t != "workspace_save@1")
    if enabled:
        service_configuration()
        approvals += ("workspace_save@1",)
    return ExecutionSnapshot.model_validate(
        {
            **config.model_dump(),
            "workspace_enabled": enabled,
            "workspace_mode": None,
            "approval_tools": approvals,
        }
    )


def workspace_request(claim, tool, arguments):
    if not workspace_policy().permits(claim.run.scope):
        raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
    try:
        origin, token = service_configuration()
        with httpx.Client(timeout=httpx.Timeout(200, connect=5), trust_env=False) as client:
            with client.stream(
                "POST",
                origin + "/api/internal/workspace/execute",
                headers={"Authorization": "Bearer " + token},
                json={
                    "runId": claim.run.run_id,
                    "attemptId": claim.attempt_id,
                    "tool": tool,
                    "arguments": arguments,
                },
            ) as response:
                if response.status_code != 200:
                    raise RuntimeFailure(
                        RunErrorCode.TOOL_NOT_ALLOWED
                        if response.status_code == 403
                        else RunErrorCode.TOOL_EXECUTION_FAILED
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 14_100_000:
                        raise ValueError()
        import json

        return json.loads(data)
    except RuntimeFailure:
        raise
    except Exception:
        raise RuntimeFailure(RunErrorCode.TOOL_EXECUTION_FAILED) from None

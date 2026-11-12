"""Recheck the API reservation around every fixed desktop file operation."""

import httpx
from agentforge_connectors.browser import service_configuration

from agentforge_supervisor.desktop_slot import execute_workspace


def transfer(generation, body):
    origin, token = service_configuration()
    if body["action"] not in {
        "saveArtifact",
        "downloadFile",
        "files",
        "readText",
        "writeText",
        "writeTextV2",
    }:
        return {"error": "workspace_invalid"}
    with httpx.Client(timeout=5, trust_env=False) as client:

        def check():
            response = client.post(
                origin + "/api/internal/workspace/check",
                headers={"Authorization": "Bearer " + token},
                json={"operationId": body["operationId"]},
            )
            response.raise_for_status()

        try:
            check()
            reply = execute_workspace(body["action"], body["request"], generation)
            check()
            return reply
        except Exception:
            return {"error": "workspace_unavailable"}

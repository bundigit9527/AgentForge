"""Authenticated Streamable HTTP endpoint for the isolated computation tool."""

import os

import anyio
from agentforge_core.reports import EXPORT_TOOL, CsvSummary
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from agentforge_supervisor.auth import RequireWorkerToken, service_token
from agentforge_supervisor.computer import DockerComputer, Summary, Values


def create_app():
    token = service_token("AGENTFORGE_MCP_COMPUTER_TOKEN")
    computer = DockerComputer(os.environ.get("AGENTFORGE_COMPUTER_IMAGE", ""))
    server = MCPServer("AgentForge Docker Computer")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def summarize_numbers(values: Values) -> Summary:
        """Calculate the count, minimum, maximum, total and mean of supplied integers."""
        return await anyio.to_thread.run_sync(computer.summarize, values)

    @server.tool(
        name=EXPORT_TOOL, annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True)
    )
    async def export_csv(values: Values) -> CsvSummary:
        """Calculate a numeric summary and return a CSV report for the chat attachment."""
        summary = await anyio.to_thread.run_sync(computer.summarize, values)
        return CsvSummary(**summary.model_dump(), csv=summary.csv_text())

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    if image := os.environ.get("AGENTFORGE_PYTHON_IMAGE"):
        from agentforge_supervisor.python_endpoint import mount_python

        mount_python(app, image)
    app.add_middleware(RequireWorkerToken, token=token.encode("ascii"))
    return app

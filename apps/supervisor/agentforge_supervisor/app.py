"""Authenticated Streamable HTTP endpoint for the isolated computation tool."""

import hmac
import os

import anyio
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from starlette.responses import Response

from agentforge_supervisor.computer import DockerComputer, Summary, Values


class RequireWorkerToken:
    def __init__(self, app, token: bytes):
        self.app = app
        self.expected = b"Bearer " + token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
            if len(values) != 1 or not hmac.compare_digest(values[0], self.expected):
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_app():
    token = os.environ.get("AGENTFORGE_MCP_COMPUTER_TOKEN", "")
    if not (
        32 <= len(token) <= 256 and token.isascii() and token.isprintable() and " " not in token
    ):
        raise ValueError("Configure a 32–256 character AGENTFORGE_MCP_COMPUTER_TOKEN")
    computer = DockerComputer(os.environ.get("AGENTFORGE_COMPUTER_IMAGE", ""))
    server = MCPServer("AgentForge Docker Computer")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def summarize_numbers(values: Values) -> Summary:
        """Calculate the count, minimum, maximum, total and mean of supplied integers."""
        return await anyio.to_thread.run_sync(computer.summarize, values)

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    app.add_middleware(RequireWorkerToken, token=token.encode("ascii"))
    return app

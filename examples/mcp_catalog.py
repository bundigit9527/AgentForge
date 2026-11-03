"""Explicitly started, loopback-only MCP demo with fictional, read-only data."""

from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

server = MCPServer("AgentForge demo catalog")


@server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def product_lookup(query: Annotated[str, Field(min_length=1, max_length=80)]) -> dict[str, str]:
    """Look up DEMO-001 in a fictional product catalog; no external data or writes."""
    if query == "DEMO-001":
        return {"code": query, "name": "示例产品", "description": "仅用于 MCP 联调的虚构数据"}
    return {"code": query, "message": "示例目录中没有该产品"}


if __name__ == "__main__":
    server.run(transport="streamable-http", host="127.0.0.1", port=8765, stateless_http=True)

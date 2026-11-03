"""Bounded Streamable HTTP client using the official MCP Python SDK.

Each discovery/call owns its own connection; no cross-user session cache, subprocess,
server-supplied instructions, OAuth flow, sampling, roots or resource fetches.
"""

import logging
from contextvars import ContextVar

import anyio
import httpx2
from agentforge_core.mcp import McpCancelled, McpFailure, McpToolSnapshot, bounded_json
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from agentforge_connectors.mcp_config import McpSettings

_private_logs = ContextVar("agentforge_mcp_private_logs", default=False)


class _PrivateLogs(logging.Filter):
    def filter(self, record):
        return not _private_logs.get()


# SDK debug/exception records can include raw messages and validation input. Suppress
# only this connector's context, preserving logs for other users of these libraries.
for _name in list(logging.Logger.manager.loggerDict):
    if _name.split(".")[0] in {"mcp", "httpx2", "httpcore2"}:
        logging.getLogger(_name).addFilter(_PrivateLogs())


class _Body(httpx2.AsyncByteStream):
    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            self.transport.total += len(chunk)
            if size > 512000 or self.transport.total > 2000000:
                raise McpFailure("mcp_invalid_response")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _Transport(httpx2.AsyncBaseTransport):
    def __init__(self, url, transport):
        self.url, self.transport, self.total = str(httpx2.URL(url)), transport, 0

    async def handle_async_request(self, request):
        if str(request.url) != self.url or request.method not in {"POST", "GET", "DELETE"}:
            raise McpFailure("mcp_not_allowed")
        response = await self.transport.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise McpFailure("mcp_invalid_response")
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise McpFailure("mcp_unavailable")
        response.stream = _Body(response.stream, self)
        return response

    async def aclose(self):
        await self.transport.aclose()


def _failure(error):
    if isinstance(error, (McpFailure, McpCancelled)):
        return error
    if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
        return McpFailure("mcp_timeout")
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            match = _failure(item)
            if match is not None:
                return match
    return None


class McpService:
    def __init__(self, settings: McpSettings | None = None, *, transport_factory=None):
        self.settings = settings or McpSettings()
        self.transport_factory = transport_factory or (lambda: httpx2.AsyncHTTPTransport(retries=0))

    def servers(self, scope):
        return [
            {"server_id": s.server_id, "tools": list(s.read_tools)}
            for s in self.settings.servers
            if s.permits(scope)
        ]

    def server(self, scope, identifier):
        server = next(
            (s for s in self.settings.servers if s.server_id == identifier and s.permits(scope)),
            None,
        )
        if server is None:
            raise McpFailure("mcp_not_allowed")
        return server

    def check(self, scope, snapshot):
        server = self.server(scope, snapshot.server_id)
        if server.read_tools.get(snapshot.tool_name) != snapshot.version:
            raise McpFailure("mcp_not_allowed")
        if server.fingerprint != snapshot.route_fingerprint:
            raise McpFailure("mcp_definition_changed")
        return server

    def catalog(self, scope, server_id):
        return self._run(self.server(scope, server_id), None, None, lambda: True)

    def resolve(self, scope, selections):
        catalogs = {}
        selected = []
        for entry in selections:
            if entry.server_id not in catalogs:
                catalogs[entry.server_id] = {
                    t.tool_name: t for t in self.catalog(scope, entry.server_id)
                }
            tool = catalogs[entry.server_id].get(entry.tool_name)
            if tool is None:
                raise McpFailure("mcp_not_allowed")
            if tool.sha256 != entry.sha256:
                raise McpFailure("mcp_definition_changed")
            selected.append(tool)
        if len({t.reference for t in selected}) != len(selected):
            raise McpFailure("mcp_not_allowed")
        return tuple(selected)

    def call(self, scope, snapshot, arguments, checkpoint):
        return self._run(self.check(scope, snapshot), snapshot, arguments, checkpoint)

    def _run(self, server, snapshot, arguments, checkpoint):
        return anyio.run(self.exchange, server, snapshot, arguments, checkpoint)

    async def exchange(self, server, snapshot, arguments, checkpoint):
        """Public async boundary also permits in-process SDK interoperability tests."""
        token = _private_logs.set(True)
        try:
            if not checkpoint():
                raise McpCancelled()
            credential = server.token()
            transport = _Transport(server.url, self.transport_factory())

            async def watch():
                while True:
                    if not checkpoint():
                        raise McpCancelled()
                    await anyio.sleep(0.1)

            with anyio.fail_after(server.timeout_seconds):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(watch)
                    async with httpx2.AsyncClient(
                        transport=transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=server.timeout_seconds + 1,
                        headers={
                            "Accept-Encoding": "identity",
                            **({"Authorization": f"Bearer {credential}"} if credential else {}),
                        },
                    ) as http:
                        async with Client(
                            streamable_http_client(
                                server.url, http_client=http, max_sse_event_size=512000
                            ),
                            read_timeout_seconds=server.timeout_seconds + 1,
                            cache=None,
                        ) as client:
                            tools = await self._catalog(client, server)
                            if snapshot is None:
                                result = tools
                            else:
                                current = next(
                                    (t for t in tools if t.tool_name == snapshot.tool_name), None
                                )
                                if current != snapshot:
                                    raise McpFailure("mcp_definition_changed")
                                if not checkpoint():
                                    raise McpCancelled()
                                # Low-level call avoids the high-level client's automatic
                                # header recovery / input-required tool replays.
                                raw = await client.session.call_tool(snapshot.tool_name, arguments)
                                if not checkpoint():
                                    raise McpCancelled()
                                result = self._result(raw, snapshot)
                    tasks.cancel_scope.cancel()
                    return result
        except Exception as exc:
            failure = _failure(exc) or McpFailure()
            raise failure from None
        finally:
            _private_logs.reset(token)

    async def _catalog(self, client, server):
        cursor, seen_cursors, seen_names, selected = None, set(), set(), []
        for _ in range(8):
            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
            if len(page.tools) > 128 or len(seen_names) + len(page.tools) > 256:
                raise McpFailure("mcp_invalid_response")
            for tool in page.tools:
                if tool.name in seen_names:
                    raise McpFailure("mcp_invalid_response")
                seen_names.add(tool.name)
                if tool.name not in server.read_tools:
                    continue
                hints = tool.annotations
                if (
                    hints is None
                    or hints.read_only_hint is not True
                    or hints.idempotent_hint is not True
                ):
                    raise McpFailure("mcp_not_allowed")
                # readOnlyHint takes precedence over destructiveHint in the MCP spec;
                # access is still granted only by the deployment-owned read_tools list.
                fields = dict(
                    server_id=server.server_id,
                    tool_name=tool.name,
                    version=server.read_tools[tool.name],
                    route_fingerprint=server.fingerprint,
                    description=tool.description or tool.name,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                )
                try:
                    selected.append(
                        McpToolSnapshot(**fields, sha256=McpToolSnapshot.digest(fields))
                    )
                except Exception:
                    raise McpFailure("mcp_invalid_response") from None
            cursor = page.next_cursor
            if cursor is None:
                return tuple(selected)
            if not cursor or len(cursor) > 1024 or cursor in seen_cursors:
                raise McpFailure("mcp_invalid_response")
            seen_cursors.add(cursor)
        raise McpFailure("mcp_invalid_response")

    @staticmethod
    def _result(raw, snapshot):
        if not isinstance(raw, CallToolResult) or raw.is_error:
            raise McpFailure("mcp_tool_failed")
        if any(not isinstance(block, TextContent) for block in raw.content):
            raise McpFailure("mcp_invalid_response")
        result = {
            "text": "\n".join(block.text for block in raw.content),
            "structured": raw.structured_content,
        }
        try:
            bounded_json(result)
            if snapshot.output_schema is not None:
                Draft202012Validator(snapshot.output_schema).validate(raw.structured_content)
        except Exception:
            # All result/schema errors are intentionally stripped of remote data.
            raise McpFailure("mcp_invalid_response") from None
        if not result["text"] and result["structured"] is None:
            raise McpFailure("mcp_invalid_response")
        return result

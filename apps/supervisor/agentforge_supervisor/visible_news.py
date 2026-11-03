"""Supervisor for a fixed, read-only news workflow in the assigned on-screen Chromium."""

import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import Annotated

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from agentforge_supervisor.auth import RequireWorkerToken, service_token

HOME = "https://www.chinanews.com.cn/finance/"
ARTICLE = re.compile(
    r"https://www\.chinanews\.com\.cn/[\w-]+/\d{4}/\d{2}-\d{2}/\d+\.shtml", re.ASCII
)
DRIVER = Path(__file__).with_name("news_tab.py")


async def document(client, request, index):
    url = request["url"]
    if request["method"] != "GET" or not (url == HOME if index == 0 else ARTICLE.fullmatch(url)):
        raise ValueError("Browser requested an unapproved document")
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            raise ValueError("Publisher returned a non-HTML document")
        data = bytearray()
        async for block in response.aiter_bytes(chunk_size=16384):
            data.extend(block)
            if len(data) > 1024 * 1024:
                raise ValueError("Publisher document exceeds limit")
    return {
        "body": base64.b64encode(data).decode("ascii"),
        "headers": [
            {"name": "Content-Type", "value": content_type},
            {
                "name": "Content-Security-Policy",
                "value": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'none'; frame-src 'none'; base-uri 'none'"
                ),
            },
            {"name": "Cache-Control", "value": "no-store"},
        ],
    }


class BrowserReader:
    def __init__(self, manifest, secret):
        self.manifest = manifest
        self.secret = secret
        self.target = None
        self.generation = None
        self.lock = asyncio.Lock()

    async def read(self, keyword):
        if self.lock.locked():
            raise RuntimeError("Browser is busy")
        async with self.lock, httpx.AsyncClient(trust_env=False, timeout=8) as api:
            entry = json.loads(self.manifest.read_text())
            generation = entry["generation"]
            if not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Invalid desktop generation")
            if generation != self.generation:
                self.target = None
                self.generation = generation
            endpoint = "http://127.0.0.1:8000/api/internal/desktop-reader"
            headers = {"Authorization": "Bearer " + self.secret}
            permission = await api.post(endpoint, json={"generation": generation}, headers=headers)
            if permission.status_code == 409:
                return "未检索：当前桌面已被接管或繁忙。请交还控制权后重新发送请求。"
            permission.raise_for_status()
            lease = permission.json()["lease"]
            try:
                async with asyncio.timeout(32):
                    return await self.drive("agentforge-desktop-" + generation, keyword)
            finally:
                await api.post(
                    endpoint, json={"generation": generation, "release": lease}, headers=headers
                )

    async def drive(self, name, keyword):
        envargs = ["docker", "exec", "-e", "HOME=/tmp/home", "-e", "DISPLAY=:1"]
        start = await asyncio.create_subprocess_exec(
            *envargs,
            "-d",
            name,
            "sh",
            "-c",
            "curl -fsS http://127.0.0.1:9222/json/version >/dev/null || "
            "exec rakazo-browser about:blank >/tmp/browser.log 2>&1",
        )
        if await start.wait() != 0:
            raise RuntimeError("Cannot launch desktop browser")
        proc = await asyncio.create_subprocess_exec(
            *envargs,
            "-i",
            name,
            "timeout",
            "30",
            "python3",
            "-I",
            "-c",
            DRIVER.read_text(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=16384,
        )

        async def send(value):
            proc.stdin.write((json.dumps(value) + "\n").encode())
            await proc.stdin.drain()

        try:
            await send({"keyword": keyword.strip(), "target": self.target})
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                for i in range(3):
                    line = await proc.stdout.readline()
                    if not line:
                        raise RuntimeError("The visible browser did not finish the page")
                    msg = json.loads(line)
                    if "target" in msg:
                        if i < 1 or (
                            msg["items"] and (i != 2 or msg["opened"] != msg["items"][0][2])
                        ):
                            raise ValueError("Article navigation did not complete")
                        self.target = msg["target"]
                        lines = [f"中新网财经，{msg['date']}（北京时间）。桌面浏览器实查。"]
                        for stamp, title, link in msg["items"][:5]:
                            if not ARTICLE.fullmatch(link) or len(title) > 200:
                                raise ValueError("Invalid browser result")
                            lines.append(f"{stamp} | {title} | {link}")
                        if not msg["items"]:
                            lines.append("当前频道页未找到当天符合条件的新闻。")
                        return "\n".join(lines)
                    if i >= 2:
                        raise ValueError("Unexpected additional navigation")
                    await send(await document(client, msg, i))
            raise RuntimeError("Missing browser result")
        finally:
            proc.stdin.close()
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


def create_app():
    reader = BrowserReader(
        Path(os.environ["AGENTFORGE_DESKTOP_MANIFEST"]),
        service_token("AGENTFORGE_DESKTOP_READER_TOKEN"),
    )
    server = MCPServer("Assigned Chromium news reader")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def browse_finance_news(keyword: Annotated[str, Field(max_length=60)] = "") -> str:
        """Read today's financial news in the assigned visible Chromium and open the newest match.
        For a general news request leave keyword empty. Report the returned Beijing times and URLs.
        """
        return await reader.read(keyword)

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    app.add_middleware(
        RequireWorkerToken, token=service_token("AGENTFORGE_MCP_SCREENNEWS_TOKEN").encode()
    )
    return app

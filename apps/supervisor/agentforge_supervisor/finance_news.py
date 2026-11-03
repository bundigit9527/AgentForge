"""Read-only MCP service for today's China News Service finance headlines."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from agentforge_supervisor.auth import RequireWorkerToken, service_token

FEED = "https://www.chinanews.com.cn/rss/finance.xml"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ARTICLE = re.compile(r"https://www\.chinanews\.com\.cn/[A-Za-z0-9_./-]+\.shtml")
MAX_BYTES = 256 * 1024


def todays_headlines(payload: bytes, now: datetime, keyword: str) -> str:
    if len(payload) > MAX_BYTES or b"<!DOCTYPE" in payload.upper() or b"\x00" in payload:
        raise ValueError("Invalid finance feed")
    # This publisher declares UTF-8. Decode before parsing to exclude alternate encodings.
    channel = ET.fromstring(payload.decode("utf-8")).find("channel")
    if channel is None or channel.find("title") is None:
        raise ValueError("Invalid finance feed")
    today = now.astimezone(SHANGHAI).date()
    rows = []
    for item in channel.findall("item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError, OverflowError):
            continue
        if not when or not when.tzinfo:
            continue
        when = when.astimezone(SHANGHAI)
        if (
            when.date() != today
            or when > now
            or not title
            or len(title) > 200
            or keyword.casefold() not in title.casefold()
            or not ARTICLE.fullmatch(link)
            or len(link) > 350
        ):
            continue
        rows.append((when, title, link))
    lines = [f"来源：中新网财经。日期：{today}（北京时间）。按发布时间倒序。"]
    seen = set()
    for when, title, link in sorted(rows, reverse=True):
        if link not in seen:
            lines.append(f"{when:%H:%M} | {title} | {link}")
            seen.add(link)
        if len(seen) == 5:
            break
    if not seen:
        lines.append("该来源当前订阅列表中未找到当天符合条件的条目。")
    return "\n".join(lines)


async def fetch_headlines(keyword: str) -> str:
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        async with client.stream("GET", FEED) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("Finance feed exceeds size limit")
    return todays_headlines(bytes(data), datetime.now(SHANGHAI), keyword.strip())


def create_app():
    token = service_token("AGENTFORGE_MCP_NEWS_TOKEN")
    server = MCPServer("AgentForge Finance News")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def latest_finance_news(keyword: Annotated[str, Field(max_length=60)] = "") -> str:
        """Read today's finance headlines from China News Service with Beijing times and URLs.

        Leave keyword empty for general news. A short keyword filters titles locally.
        """
        return await fetch_headlines(keyword)

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    app.add_middleware(RequireWorkerToken, token=token.encode("ascii"))
    return app

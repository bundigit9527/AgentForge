"""Bounded anonymous HTTPS GETs with DNS pinning and per-hop authorization."""

import http.client
import ipaddress
import os
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit

from agentforge_core.browser import public_https_url
from agentforge_core.models import RunErrorCode
from agentforge_runtime.errors import RuntimeFailure


class PublicHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        proxy = os.environ.get("AGENTFORGE_WEB_PROXY_URL", "")
        target = (self.address, 443)
        if proxy:
            parsed = urlsplit(proxy)
            if (
                parsed.scheme != "http"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
            ):
                raise ValueError("Invalid deployment web proxy")
            target = (parsed.hostname, parsed.port or 80)
        raw = socket.create_connection(target, timeout=self.timeout)
        try:
            if proxy:
                # CONNECT to the already checked public IP, never let the proxy
                # resolve the destination to a different/internal address.
                self.set_tunnel(self.address, 443)
                self.sock = raw
                self._tunnel()
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def resolve_public(host):
    addresses = list(
        dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise RuntimeFailure(RunErrorCode.WEB_NOT_ALLOWED)
    # Prefer IPv4 while validating every returned address, including IPv6.
    return sorted(addresses, key=lambda ip: ipaddress.ip_address(ip).version)


def fetch_public(url, guard, *, max_bytes=1048576, timeout=15):
    deadline = time.monotonic() + timeout
    seen = set()
    for _ in range(4):
        try:
            public_https_url(url)
        except ValueError:
            raise RuntimeFailure(RunErrorCode.WEB_NOT_ALLOWED) from None
        guard(url)
        if url in seen:
            raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE)
        seen.add(url)
        parsed = urlsplit(url)
        try:
            addresses = resolve_public(parsed.hostname)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            guard(url)
            connection = PublicHTTPS(parsed.hostname, addresses[0], min(4, remaining))
            try:
                path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
                if parsed.query:
                    path += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Host": f"[{parsed.hostname}]"
                        if ":" in parsed.hostname
                        else parsed.hostname,
                        "Accept": (
                            "text/html,application/xhtml+xml,text/plain,application/json,"
                            "application/rss+xml,application/xml;q=0.8"
                        ),
                        "Accept-Encoding": "identity",
                        "User-Agent": "AgentForge/0.1 (public web tools)",
                    },
                )
                response = connection.getresponse()
                guard(url)
                if response.status in {301, 302, 303, 307, 308}:
                    target = response.getheader("Location", "")
                    if not target:
                        raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE)
                    url = urljoin(url, target)
                    continue
                if response.status != 200:
                    raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE)
                mime = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                if mime not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                    "application/json",
                    "application/rss+xml",
                    "application/xml",
                    "text/xml",
                }:
                    raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
                if response.getheader("Content-Encoding", "identity").lower() not in (
                    "",
                    "identity",
                ):
                    raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
                body = bytearray()
                while True:
                    guard(url)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    if connection.sock is not None:
                        connection.sock.settimeout(min(4, remaining))
                    part = response.read1(16384)
                    if not part:
                        break
                    if len(body) + len(part) > max_bytes:
                        raise RuntimeFailure(RunErrorCode.WEB_LIMIT_EXCEEDED)
                    body.extend(part)
                guard(url)
                charset = response.headers.get_content_charset() or "utf-8"
                # Text is data: decoding never imports arbitrary codec implementations.
                if charset.lower() not in {
                    "utf-8",
                    "utf8",
                    "gb18030",
                    "gbk",
                    "gb2312",
                    "big5",
                    "iso-8859-1",
                    "windows-1252",
                }:
                    charset = "utf-8"
                return url, bytes(body).decode(charset, errors="replace"), mime
            finally:
                connection.close()
        except TimeoutError:
            raise RuntimeFailure(RunErrorCode.WEB_TIMEOUT) from None
        except (OSError, http.client.HTTPException):
            raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE) from None
        except ValueError:
            raise RuntimeFailure(RunErrorCode.WEB_NOT_ALLOWED) from None
    raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE)

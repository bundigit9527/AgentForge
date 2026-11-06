"""Guest-only HTML broker: exact origins, public IP pinning, TLS, bounded GET responses."""

import base64
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from agentforge_core.browser import permitted_url

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "form-action 'none'; frame-src 'none'; base-uri 'none'; sandbox allow-same-origin"
)


def public_addresses(host):
    addresses = list(
        dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Browser destination is not public")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=4, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_document(url, origins):
    if not permitted_url(url, origins):
        raise ValueError("Origin is not approved")
    p = urlsplit(url)
    ip = public_addresses(p.hostname)[0]
    connection = PinnedHTTPS(p.hostname, ip)
    deadline = time.monotonic() + 10
    try:
        path = (p.path or "/") + ("?" + p.query if p.query else "")
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "text/html",
                "Accept-Encoding": "identity",
                "User-Agent": "AgentForge-GuestBrowser/1.0",
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            from urllib.parse import urljoin

            destination = urljoin(url, response.getheader("Location", ""))
            if not permitted_url(destination, origins):
                raise ValueError("Redirect not approved")
            return {
                "status": response.status,
                "headers": [{"name": "Location", "value": destination}],
                "body": "",
            }
        mime = response.getheader("Content-Type", "")
        if response.status != 200 or mime.split(";")[0].strip().lower() != "text/html":
            raise ValueError("Expected a successful HTML response")
        if response.getheader("Content-Encoding", "identity") not in ("", "identity"):
            raise ValueError("Encoded response not supported")
        content = bytearray()
        while block := response.read1(16384):
            content.extend(block)
            if len(content) > 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("Document budget exceeded")
        return {
            "status": 200,
            "body": base64.b64encode(content).decode("ascii"),
            "headers": [
                {"name": "Content-Type", "value": mime},
                {"name": "Content-Security-Policy", "value": CSP},
                {"name": "Cache-Control", "value": "no-store"},
            ],
        }
    finally:
        connection.close()

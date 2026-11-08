"""Pinned DNS transport for user-supplied model endpoints, including streamed replies."""

import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlsplit

import httpx
from agentforge_core.model_management import ModelManagementError


def endpoint(value):
    try:
        p = urlsplit(value.strip().rstrip("/"))
        if (
            p.scheme not in {"https", "http"}
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
            or "\\" in value
            or any(ord(c) <= 32 for c in value)
            or not p.hostname.isascii()
        ):
            raise ValueError()
        _ = p.port
        origin = f"{p.scheme}://{p.netloc}"
        allowed = json.loads(os.environ.get("AGENTFORGE_MODEL_ALLOWED_ORIGINS", "[]"))
        if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
            raise ValueError()
        if p.scheme != "https" and origin not in allowed:
            raise ValueError()
        return value.strip().rstrip("/")
    except (ValueError, TypeError):
        raise ModelManagementError(
            "Use a public HTTPS endpoint, or an operator-approved origin"
        ) from None


class ModelEndpointTransport(httpx.BaseTransport):
    def __init__(self, base_url):
        self.base_url = endpoint(base_url)
        self.inner = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        base = httpx.URL(endpoint(self.base_url))
        if (request.url.scheme, request.url.host, request.url.port) != (
            base.scheme,
            base.host,
            base.port,
        ):
            raise ModelManagementError("Model endpoint changed")
        origin = f"{base.scheme}://{base.netloc.decode()}"
        allowed = json.loads(os.environ.get("AGENTFORGE_MODEL_ALLOWED_ORIGINS", "[]"))
        try:
            addresses = sorted(
                {
                    a[4][0]
                    for a in socket.getaddrinfo(
                        base.host, base.port or 443, type=socket.SOCK_STREAM
                    )
                }
            )
            if not addresses or (
                origin not in allowed
                and any(not ipaddress.ip_address(a).is_global for a in addresses)
            ):
                raise ValueError()
        except (OSError, ValueError):
            raise ModelManagementError(
                "Model endpoint could not be resolved to an allowed address"
            ) from None
        # TLS verifies the original hostname; the connection itself uses the checked IP.
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(host=addresses[0]),
            headers={**dict(request.headers), "host": base.netloc.decode()},
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": base.host},
        )
        return self.inner.handle_request(forwarded)

    def close(self):
        self.inner.close()


def discover(base_url, key):
    base_url = endpoint(base_url)
    if key and (not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key)):
        raise ModelManagementError("Invalid API key")
    try:
        with httpx.Client(
            transport=ModelEndpointTransport(base_url),
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "GET",
                base_url + "/models",
                headers={"Authorization": "Bearer " + key} if key else {},
            ) as response:
                if response.status_code != 200:
                    raise ModelManagementError(
                        "Could not list models; check the endpoint and credentials"
                    )
                started = time.monotonic()
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if time.monotonic() - started > 15:
                        raise ModelManagementError("Model catalog request timed out")
                    if len(body) > 1048576:
                        raise ModelManagementError("Model catalog is too large")
                data = json.loads(body)
        models = sorted(
            {
                m["id"]
                for m in data["data"]
                if isinstance(m, dict)
                and isinstance(m.get("id"), str)
                and 0 < len(m["id"]) <= 128
                and not any(ord(c) < 33 for c in m["id"])
            }
        )
        if not models or len(models) > 1000:
            raise ValueError()
        return models
    except ModelManagementError:
        raise
    except Exception:
        raise ModelManagementError(
            "Could not read the model catalog; check the endpoint and credentials"
        ) from None

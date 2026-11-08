"""Opt-in actual oRPC client/Zod contract verification, without TCP or an API process."""

import asyncio
import json
import os
import selectors
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from agentforge_api.orpc import sse
from agentforge_storage.passwords import PasswordLogin
from agentforge_worker.worker import Worker
from fastapi import Request

from tests.test_browser_auth import browser_client as _browser_client
from tests.test_browser_auth import login
from tests.test_identity import people as _people

browser_client = _browser_client
people = _people


def test_real_rakazo_client_and_contracts(client, repository):
    run_bridge(client, repository)


def test_real_better_auth_session_client(browser_client, repository, people):
    login(browser_client, people[2]["alice"]["token"])
    run_bridge(browser_client, repository, browser=True)


def test_original_password_client(browser_client, repository, people):
    PasswordLogin(people[0]).set_password(
        people[1]["alice"], "native@example.test", "test-native-password-only"
    )
    run_bridge(browser_client, repository, browser=True, native=True)


def run_bridge(
    client,
    repository,
    *,
    browser=False,
    native=False,
    model_chat=False,
    worker=None,
    stream_control=None,
    approval_mode=False,
    model_management=False,
):
    configured = os.environ.get("AGENTFORGE_RAKAZO_ROOT")
    if not configured:
        pytest.skip("Set AGENTFORGE_RAKAZO_ROOT for pinned Rakazo client interoperability")
    root = Path(configured)
    loader = root / "node_modules/tsx/dist/loader.mjs"
    assert loader.is_file(), "Install the pinned Rakazo dependencies first"
    script = Path(__file__).parent / "interop/rakazo_client.mts"

    async def stream_response(message):
        body = message["body"].encode()

        async def receive():
            nonlocal body
            content, body = body, b""
            return {"type": "http.request", "body": content, "more_body": False}

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "scheme": "http",
                "method": "POST",
                "path": "/rpc/threads/subscribe",
                "query_string": b"",
                "headers": [(k.encode(), v.encode()) for k, v in message["headers"].items()],
            },
            receive,
        )
        endpoint = next(
            route.endpoint for route in client.app.routes if route.path == "/rpc/{procedure:path}"
        )
        response = await endpoint("threads/subscribe", request)
        if response.status_code != 200:
            return {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": response.body.decode(),
            }
        # Drain the real route's generator through the current durable head, then
        # close the test transport. Production subscriptions stay open for later runs.
        data = json.loads(message["body"])["json"]
        scope = client.app.state.settings.dev_scope
        head = client.app.state.rakazo.head(scope, data["botId"])["cursor"]
        chunks = []
        try:
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if chunk.startswith(f"id: {head}\n"):
                    break
        finally:
            await response.body_iterator.aclose()
        return {
            "status": 200,
            "headers": dict(response.headers),
            "body": "".join(chunks) + sse({}, event="done"),
        }

    with subprocess.Popen(
        ["node", "--import", str(loader), str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={
            **os.environ,
            **({"AGENTFORGE_BROWSER_INTEROP": "1"} if browser else {}),
            **({"AGENTFORGE_NATIVE_LOGIN": "1"} if native else {}),
            **({"AGENTFORGE_MODEL_INTEROP": "1"} if model_chat else {}),
            **({"AGENTFORGE_STREAM_INTEROP": "1"} if stream_control else {}),
            **({"AGENTFORGE_APPROVAL_INTEROP": "1"} if approval_mode else {}),
            **({"AGENTFORGE_MODEL_MANAGEMENT_INTEROP": "1"} if model_management else {}),
        },
    ) as process:
        complete = False
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while True:
                    assert ready.select(30), "oRPC bridge timed out"
                    line = process.stdout.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    if message["type"] == "complete":
                        complete = True
                        break
                    if message["type"] == "stream":
                        result = stream_control(message)
                    elif message["type"] == "worker":
                        assert (worker or Worker(repository)).run_once()
                        result = {"ok": True}
                    elif urlsplit(message["url"]).path == "/rpc/threads/subscribe":
                        result = asyncio.run(stream_response(message))
                    else:
                        response = client.request(
                            message["method"],
                            urlsplit(message["url"]).path,
                            headers=message["headers"],
                            content=message["body"],
                        )
                        result = {
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "body": response.text,
                        }
                    process.stdin.write(json.dumps(result, ensure_ascii=False) + "\n")
                    process.stdin.flush()
            process.wait(timeout=10)
            assert process.returncode == 0 and complete, process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

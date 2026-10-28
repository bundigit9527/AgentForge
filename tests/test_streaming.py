import asyncio
import json
import re

import pytest
from agentforge_api.app import create_app
from agentforge_api.streaming import encode_event, stream_events
from agentforge_core.models import RunEvent
from agentforge_worker.worker import Worker
from sqlalchemy.exc import OperationalError


class Connection:
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def test_stream_replays_then_resumes_using_last_event_id(client, headers, repository, scope):
    run = repository.create(scope, "stream")
    Worker(repository).run_once()
    url = f"/api/v1/runs/{run.run_id}/events/stream"
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert re.findall(r"^id: (\d+)$", response.text, re.M) == ["1", "2", "3", "4"]
    resumed = client.get(f"{url}?after=1", headers={**headers, "Last-Event-ID": "2"})
    assert re.findall(r"^id: (\d+)$", resumed.text, re.M) == ["3", "4"]
    exhausted = client.get(url, headers={**headers, "Last-Event-ID": "4"})
    assert exhausted.status_code == 204


def test_terminal_stream_drains_all_event_pages(client, headers, repository, scope):
    run = repository.create(scope, "many events")
    claim = repository.claim_next()
    for _ in range(205):
        repository.record_tool(claim, "example_report")
    repository.finish(claim, result="done")
    response = client.get(f"/api/v1/runs/{run.run_id}/events/stream", headers=headers)
    assert list(map(int, re.findall(r"^id: (\d+)$", response.text, re.M))) == list(range(1, 209))


def test_stream_delivers_events_created_after_subscription(repository, scope):
    run = repository.create(scope, "live")

    async def collect():
        stream = stream_events(Connection(), repository, scope, run.run_id, 0, poll_interval=0.001)
        try:
            first = await asyncio.wait_for(anext(stream), 2)
            assert first.startswith("id: 1\n")
            await asyncio.to_thread(Worker(repository).run_once)
            remaining = [item async for item in stream]
            return first + "".join(remaining)
        finally:
            await stream.aclose()

    output = asyncio.run(collect())
    assert re.findall(r"^event: (.+)$", output, re.M) == [
        "queued",
        "running",
        "tool_started",
        "succeeded",
    ]


@pytest.mark.parametrize("asgi_version", ["2.0", "2.4"])
def test_live_asgi_response_delivers_progress_without_a_network_server(
    settings, repository, scope, headers, asgi_version
):
    run = repository.create(scope, "live transport")
    app = create_app(settings, repository)
    path = f"/api/v1/runs/{run.run_id}/events/stream"

    async def exercise():
        messages = []
        parked = asyncio.Event()
        initial = True

        async def receive():
            nonlocal initial
            if initial:
                initial = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await parked.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message.get("body", b"").startswith(b"id: 1\n"):
                await asyncio.to_thread(Worker(repository).run_once)

        request_scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": asgi_version},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", headers["Authorization"].encode())],
            "server": ("test", 80),
            "client": ("test", 1),
        }
        await asyncio.wait_for(app(request_scope, receive, send), 3)
        assert messages[0]["status"] == 200
        return b"".join(message.get("body", b"") for message in messages).decode()

    output = asyncio.run(exercise())
    assert re.findall(r"^id: (\d+)$", output, re.M) == ["1", "2", "3", "4"]


def test_idle_stream_heartbeat_and_disconnect_release_generator(repository, scope):
    run = repository.create(scope, "waiting")

    async def exercise():
        request = Connection()
        stream = stream_events(
            request,
            repository,
            scope,
            run.run_id,
            1,
            poll_interval=0.001,
            heartbeat_interval=0.001,
        )
        try:
            assert await asyncio.wait_for(anext(stream), 2) == ": keep-alive\n\n"
            request.disconnected = True
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 2)
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_storage_failure_does_not_leak_database_details(repository, scope, monkeypatch):
    run = repository.create(scope, "failure")

    def failed(*args):
        raise OperationalError("private-sql", {}, RuntimeError("private-credential"))

    monkeypatch.setattr(repository, "get", failed)

    async def collect():
        return "".join(
            [event async for event in stream_events(Connection(), repository, scope, run.run_id, 0)]
        )

    output = asyncio.run(collect())
    assert "storage_unavailable" in output
    assert "private" not in output


def test_json_data_cannot_inject_sse_frames():
    event = RunEvent(1, "run", "tool_started", {"text": "中文\n\nid: 999"}, 1800000000)
    encoded = encode_event(event)
    assert len(encoded.splitlines()) == 4
    assert json.loads(encoded.splitlines()[2][6:])["data"] == event.data


@pytest.mark.parametrize("cursor", ["-1", "abc", "1.5", "9" * 20, str(2**63)])
def test_invalid_last_event_id_is_rejected_before_streaming(
    client, headers, repository, scope, cursor
):
    run = repository.create(scope, "invalid cursor")
    response = client.get(
        f"/api/v1/runs/{run.run_id}/events/stream",
        headers={**headers, "Last-Event-ID": cursor},
    )
    assert response.status_code == 422


def test_stream_requires_authentication(client, repository, scope):
    run = repository.create(scope, "private")
    assert client.get(f"/api/v1/runs/{run.run_id}/events/stream").status_code == 401

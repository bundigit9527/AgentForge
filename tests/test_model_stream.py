import json
from dataclasses import replace

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_core.execution import ModelConfig
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.chat_model import ChatCompletionsModel
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_runtime.progress import TextProgress
from agentforge_storage.rakazo import RakazoRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import model_settings
from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc
from tests.test_orpc_interop import run_bridge

people = _people
enterprise = _enterprise
USAGE = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}


def chunk(content=None, *, finish=None, calls=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if calls is not None:
        delta["tool_calls"] = calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}


def frame(value):
    return (
        "data: "
        + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        + "\r\n\r\n"
    ).encode()


class Wire(httpx.SyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    def __iter__(self):
        yield from self.parts

    def close(self):
        self.closed = True


def response(parts):
    wire = Wire(parts)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)


@pytest.mark.parametrize("style,usage_style", [("deepseek", "attached"), ("openai", "separate")])
def test_stream_accepts_utf8_boundaries_keepalive_and_usage(style, usage_style):
    packets = [
        chunk("你"),
        chunk("好🙂"),
        chunk(finish="stop", usage=USAGE if usage_style == "attached" else None),
    ]
    if usage_style == "separate":
        packets.append({"choices": [], "usage": USAGE})
    wire = b": keepalive\r\n\r\n" + b"".join(map(frame, packets)) + frame("[DONE]")
    fragments = []

    def serve(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return response(wire[i : i + 1] for i in range(len(wire)))

    model = ChatCompletionsModel(
        model_settings(stream=True, api_style=style), transport=httpx.MockTransport(serve)
    )
    try:
        reply = model.complete([], [], on_text=fragments.append)
        assert reply.choices[0].message.content == "你好🙂"
        assert fragments == ["你", "好🙂"]
        assert reply.usage.total_tokens == 8
    finally:
        model.close()


def test_stream_tool_arguments_wait_for_validated_done_before_execution():
    calls = [
        chunk(
            calls=[
                {
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "example_", "arguments": '{"text":'},
                }
            ]
        ),
        chunk(calls=[{"index": 0, "function": {"name": "report", "arguments": '"资料"}'}}]),
        chunk(finish="tool_calls", usage=USAGE),
    ]
    requests, executed = [], []

    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:

            def parts():
                for item in calls:
                    assert not executed
                    yield frame(item)
                assert not executed
                yield frame("[DONE]")

            return response(parts())
        assert executed == ["example_report"]
        assert body["messages"][-2]["content"] is None
        assert body["messages"][-1]["content"].endswith("资料")
        return response(
            [frame(chunk("完成")), frame(chunk(finish="stop", usage=USAGE)), frame("[DONE]")]
        )

    with configured_harness(
        model_settings(stream=True), transport=httpx.MockTransport(serve)
    ) as harness:
        assert harness.execute("test", lambda name: executed.append(name) or True) == "完成"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "parts,code",
    [
        ([frame(chunk("partial"))], "model_invalid_response"),
        ([frame(chunk("partial", finish="stop"))], "model_invalid_response"),
        ([frame("[DONE]")], "model_invalid_response"),
        ([frame('{"bad":true}')], "model_invalid_response"),
        ([b"data: \xff\n\n"], "model_invalid_response"),
        ([frame(chunk("x", finish="stop"))[:-2]], "model_invalid_response"),
        (
            [frame(chunk("x", finish="stop")), frame(chunk("late")), frame("[DONE]")],
            "model_invalid_response",
        ),
        ([frame(chunk("x", finish="length")), frame("[DONE]")], "model_limit_exceeded"),
        ([frame(chunk("x", finish="content_filter")), frame("[DONE]")], "model_refused"),
        (
            [
                frame(chunk("x", finish="stop", usage=USAGE)),
                frame({"choices": [], "usage": USAGE}),
                frame("[DONE]"),
            ],
            "model_invalid_response",
        ),
        (
            [
                frame(
                    {
                        "choices": [
                            {"index": False, "delta": {"content": "x"}, "finish_reason": "stop"}
                        ]
                    }
                ),
                frame("[DONE]"),
            ],
            "model_invalid_response",
        ),
        (
            [
                frame(
                    chunk(
                        calls=[
                            {
                                "index": 1,
                                "id": "call",
                                "function": {"name": "example_report", "arguments": "{}"},
                            }
                        ],
                        finish="tool_calls",
                    )
                ),
                frame("[DONE]"),
            ],
            "model_invalid_response",
        ),
        (
            [
                frame(chunk(calls=[{"index": 0}, {"index": 0}], finish="tool_calls")),
                frame("[DONE]"),
            ],
            "model_invalid_response",
        ),
        (
            [
                frame(chunk("a" * 15000)),
                frame(chunk("b" * 6000)),
                frame(chunk(finish="stop")),
                frame("[DONE]"),
            ],
            "model_limit_exceeded",
        ),
        ([b":" + b"x" * (1024 * 1024)], "model_limit_exceeded"),
    ],
)
def test_bad_stream_never_returns_a_successful_completion(parts, code):
    model = ChatCompletionsModel(
        model_settings(stream=True), transport=httpx.MockTransport(lambda _: response(parts))
    )
    try:
        with pytest.raises(RuntimeFailure) as error:
            model.complete([], [])
        assert str(error.value) == code
    finally:
        model.close()


def test_cancellation_closes_the_stream_before_later_tokens():
    alive, seen = [True], []

    def parts():
        yield frame(chunk("first"))
        alive[0] = False
        yield frame(chunk("discard"))
        pytest.fail("Cancelled stream continued reading")

    wire = Wire(parts())
    model = ChatCompletionsModel(
        model_settings(stream=True),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=wire
            )
        ),
    )
    try:
        with pytest.raises(ExecutionStopped):
            model.complete([], [], on_text=seen.append, checkpoint=lambda: alive[0])
        assert seen == ["first"] and wire.closed
    finally:
        model.close()


def test_stream_settings_are_frozen_and_missing_old_field_defaults_to_false(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_MODEL_STREAM", "bad")
    with pytest.raises(ValueError):
        ModelSettings.from_env()
    monkeypatch.setenv("AGENTFORGE_MODEL_STREAM", "true")
    assert ModelSettings.from_env().stream is True
    settings = model_settings(stream=True)
    assert replace(settings, stream=False).for_snapshot(settings.snapshot()).stream is True
    old = settings.snapshot().model_dump()
    old.pop("stream")
    assert not ModelConfig.model_validate(old).stream
    with pytest.raises(ValueError):
        model_settings(stream=1)


def test_text_updates_are_coalesced_and_bounded():
    calls = []
    now = [0.0]
    buffer = TextProgress(lambda **data: calls.append(data) or True, clock=lambda: now[0])
    buffer.push(1, "a")
    for _ in range(50):
        buffer.push(1, "b")
    assert len(calls) == 1
    now[0] = 0.3
    buffer.push(1, "c")
    buffer.push(2, "second")
    buffer.flush()
    assert [c["reset"] for c in calls] == [True, False, False]
    assert "".join(c["text"] for c in calls) == "a" + "b" * 50 + "c\n\nsecond"
    buffer.push(2, "x" * 5000)
    buffer.flush()
    assert all(len(c["text"]) <= 2048 for c in calls)
    with pytest.raises(RuntimeFailure):
        buffer.push(2, "x" * 80000)


def test_live_text_is_durable_until_success_and_only_final_enters_context(
    client, headers, repository, scope
):
    bot = create_bot(client, headers)
    sent = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "question"}).json()[
        "json"
    ]
    claim = repository.claim_next()
    assert repository.record_text(claim, step=1, text="Hello", reset=True)
    service = RakazoRepository(repository)
    snap = service.snapshot(scope, bot["id"])
    assert snap["messages"][-1]["id"] == "progress:" + sent["runId"]
    assert snap["messages"][-1]["blocks"] == [{"kind": "progress", "text": "Hello"}]
    assert repository.get(scope, sent["runId"]).result is None
    cursor = snap["cursor"]
    assert repository.record_text(claim, step=1, text=" world")
    events = service.events(scope, bot["id"], cursor)
    assert [e["type"] for e in events] == ["thread.progress"]
    assert events[0]["payload"]["delta"] == " world"
    assert repository.finish(claim, result="Final answer")
    assert not repository.record_text(claim, step=1, text="late")
    saved = service.snapshot(scope, bot["id"])
    assert [m["role"] for m in saved["messages"]] == ["user", "bot"]
    assert saved["messages"][-1]["blocks"] == [{"kind": "text", "text": "Final answer"}]
    next_run = rpc(
        client, headers, "threads.send", {"botId": bot["id"], "text": "follow up"}
    ).json()["json"]
    assert [m.content for m in repository.get(scope, next_run["runId"]).context] == [
        "question",
        "Final answer",
    ]


@pytest.mark.parametrize("action", ["cancel", "clear", "expire", "retry", "fail"])
def test_stale_drafts_cannot_survive_cancellation_retry_or_clear(
    client, headers, repository, scope, clock, action
):
    bot = create_bot(client, headers)
    rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "test"})
    claim = repository.claim_next()
    repository.record_text(claim, step=1, text="Old.", reset=True)
    if action == "cancel":
        repository.cancel(scope, claim.run.run_id)
    if action == "clear":
        assert rpc(client, headers, "threads.clear", {"botId": bot["id"]}).status_code == 200
    if action in {"expire", "retry"}:
        clock.advance(61)
    if action == "fail":
        repository.finish(claim, failed=True)
    assert not repository.record_text(claim, step=1, text="stale")
    if action == "retry":
        new = repository.claim_next()
        assert new.attempt_id != claim.attempt_id
        assert not any(
            m["id"].startswith("progress:")
            for m in RakazoRepository(repository).snapshot(scope, bot["id"])["messages"]
        )
        repository.record_text(new, step=1, text="Fresh", reset=True)
        snap = RakazoRepository(repository).snapshot(scope, bot["id"])
        assert snap["messages"][-1]["blocks"] == [{"kind": "progress", "text": "Fresh"}]
    elif action != "expire":
        assert not any(
            m["id"].startswith("progress:")
            for m in RakazoRepository(repository).snapshot(scope, bot["id"])["messages"]
        )


def test_revoked_member_cannot_append_or_read_drafts(enterprise, repository):
    client, headers, identities, scopes, _ = enterprise
    bot = create_bot(client, headers["alice"])
    rpc(client, headers["alice"], "threads.send", {"botId": bot["id"], "text": "private"})
    claim = repository.claim_next()
    assert claim
    assert repository.record_text(claim, step=1, text="Private draft", reset=True)
    assert rpc(client, headers["bob"], "threads.get", {"botId": bot["id"]}).status_code == 404
    identities.set_member(
        scopes["admin"], "alice", display_name="alice", role="member", active=False
    )
    assert not repository.record_text(claim, step=1, text="late")
    assert rpc(client, headers["alice"], "threads.get", {"botId": bot["id"]}).status_code == 401


def test_worker_stream_publishes_before_completion_and_aborts_broken_reply(
    settings, repository, headers, scope
):
    model = model_settings(stream=True)
    with TestClient(create_app(settings, repository, model_settings=model)) as client:
        bot = create_bot(client, headers)
        rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "partial"})

        def parts():
            yield frame(chunk("Live text"))
            assert RakazoRepository(repository).snapshot(scope, bot["id"])["messages"][-1][
                "blocks"
            ] == [{"kind": "progress", "text": "Live text"}]
            raise httpx.ReadError("private-upstream-error")

        worker = Worker(
            repository,
            harness_factory=lambda snapshot: configured_harness(
                model.for_snapshot(snapshot.model),
                transport=httpx.MockTransport(lambda _: response(parts())),
            ),
        )
        assert worker.run_once()
        snap = RakazoRepository(repository).snapshot(scope, bot["id"])
        assert snap["run"]["status"] == "failed" and snap["run"]["error"] == "model_unavailable"
        assert len(snap["messages"]) == 1
        assert "private-upstream-error" not in json.dumps(snap)


def test_native_stream_reducer_and_reconnection(client, repository, clock):
    claim = [None]

    def control(message):
        action = message["action"]
        if claim[0] is None:
            claim[0] = repository.claim_next()
        if action == "first":
            repository.record_text(claim[0], step=1, text="Hello.", reset=True)
        elif action == "more":
            repository.record_text(claim[0], step=1, text=" World")
        elif action == "retry":
            clock.advance(61)
            claim[0] = repository.claim_next()
            repository.record_text(claim[0], step=1, text="Fresh", reset=True)
        elif action == "finish":
            repository.finish(claim[0], result="Final")
        return {"ok": True}

    run_bridge(client, repository, stream_control=control)

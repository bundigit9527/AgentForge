import json

import httpx
import pytest
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.chat_model import MAX_RESPONSE_BYTES, ChatCompletionsModel, RuntimeFailure
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.harness import ExecutionStopped, Harness

from tests.model_support import completion, model_settings, tool_call


def test_tool_results_and_usage_flow_through_two_model_rounds():
    requests, events, tools = [], [], []

    def respond(request):
        assert str(request.url) == "https://gateway.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-model-credential"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test-model"
        assert body["max_completion_tokens"] == 2048
        assert body["stream"] is False
        assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
        if len(requests) == 1:
            return httpx.Response(200, json=completion(None, [tool_call()]))
        assert body["messages"][-2]["tool_calls"][0]["id"] == "call-1"
        assert body["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "# 报告草稿\n\n资料摘要",
        }
        return httpx.Response(200, json=completion())

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        result = harness.execute(
            "整理资料",
            lambda name: tools.append(name) or True,
            on_model=lambda **data: events.append(data) or True,
        )
    assert result == "最终回答"
    assert tools == ["example_report"]
    assert [event["step"] for event in events] == [1, 2]
    assert sum(event["usage"]["total_tokens"] for event in events) == 30


def test_missing_usage_is_unknown_and_conversations_do_not_leak_between_runs():
    messages, events = [], []

    def respond(request):
        messages.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json=completion(usage=False))

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        for prompt in ["first user's task", "second user's task"]:
            assert harness.execute(
                prompt, lambda _: True, on_model=lambda **data: events.append(data) or True
            )
    assert len(messages[1]) == 2
    assert messages[1][1]["content"] == "second user's task"
    assert "first user's task" not in json.dumps(messages[1])
    assert events[0]["usage"] is None


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "model_auth_failed"),
        (403, "model_auth_failed"),
        (429, "model_rate_limited"),
        (503, "model_unavailable"),
        (302, "model_unavailable"),
        (400, "model_unavailable"),
    ],
)
def test_http_failure_is_sanitized_and_never_retried_or_redirected(status, code):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, text="private-upstream-body", headers={"Location": "https://other.example"}
        )

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute("task", lambda _: True)
    assert str(error.value) == code
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure,code",
    [(httpx.ReadTimeout, "model_timeout"), (httpx.ConnectError, "model_unavailable")],
)
def test_transport_failure_does_not_expose_provider_details(failure, code):
    def respond(request):
        raise failure("private-upstream-body", request=request)

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute("task", lambda _: True)
    assert str(error.value) == code


@pytest.mark.parametrize(
    "body,code",
    [
        ({"choices": []}, "model_invalid_response"),
        (completion(content=" "), "model_invalid_response"),
        (completion(calls=[tool_call()], finish_reason="stop"), "model_invalid_response"),
        (completion(finish_reason="tool_calls"), "model_invalid_response"),
        (completion(finish_reason="length"), "model_limit_exceeded"),
        (completion(finish_reason="content_filter"), "model_refused"),
        (
            {
                **completion(),
                "usage": {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
            },
            "model_invalid_response",
        ),
    ],
)
def test_invalid_or_incomplete_completion_is_not_success(body, code):
    with configured_harness(
        model_settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute("task", lambda _: True)
    assert str(error.value) == code


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not JSON", "model_invalid_response"),
        (b"x" * (MAX_RESPONSE_BYTES + 1), "model_limit_exceeded"),
    ],
)
def test_response_body_is_bounded(body, code):
    with configured_harness(
        model_settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute("task", lambda _: True)
    assert str(error.value) == code


@pytest.mark.parametrize(
    "calls,code",
    [
        ([tool_call(name="shell")], "tool_not_allowed"),
        ([tool_call(arguments="{bad")], "tool_invalid_arguments"),
        ([tool_call(arguments='{"text":12}')], "tool_invalid_arguments"),
        ([tool_call(arguments='{"text":"hello","tenant_id":"forged"}')], "tool_invalid_arguments"),
        ([tool_call(arguments='{"text":" "}')], "tool_invalid_arguments"),
        ([tool_call(), tool_call()], "model_invalid_response"),
        ([tool_call(), tool_call("call-2", name="shell")], "tool_not_allowed"),
    ],
)
def test_all_tools_are_validated_before_any_execution(calls, code):
    executed = []
    with configured_harness(
        model_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(None, calls))),
    ) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute("task", lambda name: executed.append(name) or True)
    assert str(error.value) == code
    assert executed == []


def test_round_limit_does_not_execute_a_tool_without_a_followup_round():
    executed, requests = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=completion(None, [tool_call(f"call-{len(requests)}")]))

    with configured_harness(
        model_settings(max_rounds=2), transport=httpx.MockTransport(respond)
    ) as harness:
        with pytest.raises(RuntimeFailure, match="model_limit_exceeded"):
            harness.execute("task", lambda name: executed.append(name) or True)
    assert len(requests) == 2
    assert executed == ["example_report"]


def test_cancelled_checkpoint_prevents_the_first_request():
    requests = []
    with configured_harness(
        model_settings(), transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as harness:
        with pytest.raises(ExecutionStopped):
            harness.execute("task", lambda _: True, checkpoint=lambda: False)
    assert requests == []


def test_default_harness_stays_offline_and_http_client_is_closed():
    with configured_harness(ModelSettings()) as harness:
        assert type(harness) is Harness
    model = ChatCompletionsModel(
        model_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
    )
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.complete([], [])


def test_reused_call_id_in_later_round_does_not_repeat_a_tool():
    executed = []
    with configured_harness(
        model_settings(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion(None, [tool_call()]))
        ),
    ) as harness:
        with pytest.raises(RuntimeFailure, match="model_invalid_response"):
            harness.execute("task", lambda name: executed.append(name) or True)
    assert executed == ["example_report"]


def test_context_limit_prevents_an_oversized_followup_request():
    requests = []

    def respond(request):
        requests.append(request)
        calls = [
            tool_call(str(index), arguments=json.dumps({"text": "x" * 10000})) for index in range(8)
        ]
        return httpx.Response(200, json=completion(None, calls))

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        with pytest.raises(RuntimeFailure, match="model_limit_exceeded"):
            harness.execute("task", lambda _: True)
    assert len(requests) == 1


def test_elapsed_response_budget_stops_reading(monkeypatch):
    import agentforge_runtime.chat_model as module

    ticks = iter([0, 31])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    model = ChatCompletionsModel(
        model_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
    )
    try:
        with pytest.raises(RuntimeFailure, match="model_timeout"):
            model.complete([], [])
    finally:
        model.close()


def test_failed_usage_callback_blocks_tools_and_transport_closes_on_error():
    executed, closed = [], []

    class Transport(httpx.MockTransport):
        def close(self):
            closed.append(True)
            super().close()

    transport = Transport(lambda _: httpx.Response(200, json=completion(None, [tool_call()])))
    with pytest.raises(ExecutionStopped):
        with configured_harness(model_settings(), transport=transport) as harness:
            harness.execute(
                "task", lambda name: executed.append(name) or True, on_model=lambda **_: False
            )
    assert executed == []
    assert closed == [True]

"""Budgeted history selection, execution ownership and immutable run inputs."""

import json
from dataclasses import replace

import httpx
import pytest
from agentforge_core.execution import ContextMessage, ExecutionSnapshot, ModelConfig
from agentforge_core.models import RunNotFound, RunStatus
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.context import OMITTED_HISTORY_NOTICE, ContextWindow, context_size
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_runtime.tools import ToolExecutor
from agentforge_storage.conversations import ConversationRepository
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import people as _people
from tests.test_model_stream import USAGE, chunk, frame, response

people = _people


def history(size=1200):
    return tuple(
        ContextMessage(role=role, content=text * size)
        for role, text in (("user", "旧"), ("assistant", "答"), ("user", "近"), ("assistant", "新"))
    )


def test_context_settings_are_validated_and_pinned(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_MODEL_CONTEXT_POLICY", "trim_history")
    monkeypatch.setenv("AGENTFORGE_MODEL_CONTEXT_MAX_CHARS", "7000")
    assert ModelSettings.from_env().context_max_chars == 7000
    gateway = model_settings(context_max_chars=7000)
    snapshot = gateway.snapshot()
    changed = replace(gateway, context_max_chars=20000, context_policy="reject")
    resolved = changed.for_snapshot(snapshot)
    assert resolved.snapshot() == snapshot
    assert resolved.context_policy == "trim_history"
    old = snapshot.model_dump(exclude={"context_policy", "context_max_chars"})
    old_snapshot = ModelConfig.model_validate(old)
    assert old_snapshot.context_policy == "reject"
    assert old_snapshot.context_max_chars == 100000
    assert changed.for_snapshot(old_snapshot).snapshot() == old_snapshot
    assert ModelSettings().snapshot() == ModelConfig()
    monkeypatch.setenv("AGENTFORGE_MODEL_CONTEXT_MAX_CHARS", "private-value")
    with pytest.raises(ValueError) as error:
        ModelSettings.from_env()
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("limit", [True, 3999, 200001, 5000.0])
def test_invalid_context_limits_fail_before_execution(limit):
    with pytest.raises(ValueError):
        model_settings(context_max_chars=limit)
    with pytest.raises(ValueError):
        ModelConfig(context_max_chars=limit)


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        model_settings(context_policy="summarize")
    with pytest.raises(ValueError):
        ModelConfig(context_policy="summarize")


def test_history_is_trimmed_in_complete_turns_without_mutating_snapshot():
    original = history()
    window = ContextWindow(
        [{"role": "system", "content": "keep instructions"}],
        original,
        "current question",
        [],
        config=ModelConfig(context_policy="trim_history", context_max_chars=4000),
    )
    messages, decision = window.fit()
    assert [m["content"] for m in messages] == [
        "keep instructions",
        OMITTED_HISTORY_NOTICE,
        "近" * 1200,
        "新" * 1200,
        "current question",
    ]
    assert decision["dropped_turns"] == decision["retained_turns"] == 1
    assert decision["after_chars"] == context_size(messages, []) <= 4000
    assert window.fit() == (messages, None)
    assert original == history()


def test_tool_schemas_count_towards_new_policy_budget():
    tools = [{"description": "schema" * 600}]
    window = ContextWindow(
        [],
        history(100),
        "current",
        tools,
        config=ModelConfig(context_policy="trim_history", context_max_chars=4000),
    )
    messages, decision = window.fit()
    assert decision is not None
    assert context_size(messages, tools) <= 4000
    assert tools == [{"description": "schema" * 600}]


@pytest.mark.parametrize("roles", [["assistant"], ["assistant", "user"], ["user", "user"]])
def test_malformed_history_fails_without_sending_an_orphan_reply(roles):
    context = tuple(ContextMessage(role=role, content="x") for role in roles)
    with pytest.raises(RuntimeFailure, match="execution_failed"):
        ContextWindow([], context, "task", [], config=ModelConfig())


def test_budget_boundary_and_unicode_are_deterministic():
    initial = [{"role": "user", "content": '中文🙂"\\' * 1000}]
    size = context_size(initial, [])
    config = ModelConfig(context_policy="trim_history", context_max_chars=size)
    window = ContextWindow([], (), initial[0]["content"], [], config=config)
    assert window.fit() == (initial, None)
    window = ContextWindow(
        [],
        (),
        initial[0]["content"],
        [],
        config=config.model_copy(update={"context_max_chars": size - 1}),
    )
    with pytest.raises(RuntimeFailure, match="model_context_limit_exceeded"):
        window.fit()


@pytest.mark.parametrize("protected", ["instructions", "prompt", "tool_schema", "tool_result"])
def test_protected_context_overflow_never_truncates_data(protected):
    prefix = [{"role": "system", "content": "x" * 4500}] if protected == "instructions" else []
    prompt = "x" * 4500 if protected == "prompt" else "current"
    tools = [{"description": "x" * 4500}] if protected == "tool_schema" else []
    window = ContextWindow(
        prefix,
        history(10),
        prompt,
        tools,
        config=ModelConfig(context_policy="trim_history", context_max_chars=4000),
    )
    if protected == "tool_result":
        window.current.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [tool_call()]},
                {"role": "tool", "tool_call_id": "call-1", "content": "x" * 4500},
            ]
        )
    original = json.dumps([window.prefix, window.history, window.current, tools])
    with pytest.raises(RuntimeFailure, match="model_context_limit_exceeded"):
        window.fit()
    assert json.dumps([window.prefix, window.history, window.current, tools]) == original


def test_old_policy_keeps_original_counting_and_error_without_trimming():
    window = ContextWindow([], history(), "current", [], config=ModelConfig(context_max_chars=4000))
    with pytest.raises(RuntimeFailure, match="^model_limit_exceeded$"):
        window.fit()
    assert len(window.history) == 4
    # Old versions did not include tool schema size in the context count.
    window = ContextWindow(
        [], (), "task", [{"description": "x" * 5000}], config=ModelConfig(context_max_chars=4000)
    )
    assert window.fit()[1] is None


@pytest.mark.parametrize("streaming", [False, True])
def test_tool_loop_trims_history_preserving_tool_pair_and_current_prompt(streaming):
    requests, decisions, text = [], [], []
    gateway = model_settings(context_max_chars=7000, stream=streaming)
    original = history()
    current = "请依据工具结果回答"
    arguments = json.dumps({"text": "工" * 1200}, ensure_ascii=False)

    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if streaming:
                return response(
                    [
                        frame(
                            chunk(
                                calls=[
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "example_report",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            )
                        ),
                        frame(chunk(finish="tool_calls", usage=USAGE)),
                        frame("[DONE]"),
                    ]
                )
            return httpx.Response(200, json=completion(None, [tool_call(arguments=arguments)]))
        if streaming:
            return response(
                [frame(chunk("done")), frame(chunk(finish="stop", usage=USAGE)), frame("[DONE]")]
            )
        return httpx.Response(200, json=completion("done"))

    with configured_harness(gateway, transport=httpx.MockTransport(serve)) as harness:
        assert (
            harness.execute(
                current,
                lambda _: True,
                context=original,
                execution_config=ExecutionSnapshot(
                    model=gateway.snapshot(), instructions="use Chinese"
                ),
                on_context=lambda **data: decisions.append(data) or True,
                on_text=lambda **data: text.append(data) or True,
            )
            == "done"
        )
    assert len(requests) == 2
    assert decisions[0]["step"] == 2
    assert decisions[0]["dropped_turns"] == 1
    second = requests[1]["messages"]
    assert second[1] == {"role": "system", "content": "use Chinese"}
    assert second[-3] == {"role": "user", "content": current}
    assert second[-2]["tool_calls"][0]["id"] == second[-1]["tool_call_id"] == "call-1"
    assert second[-1]["content"].endswith("工" * 1200)
    assert original == history()
    assert len(text) == (1 if streaming else 0)


def test_trimmed_context_callback_must_still_own_the_execution():
    sent = []
    with configured_harness(
        model_settings(context_max_chars=4000),
        transport=httpx.MockTransport(lambda req: sent.append(req)),
    ) as harness:
        with pytest.raises(ExecutionStopped):
            harness.execute("task", lambda _: True, context=history(), on_context=lambda **_: False)
    assert sent == []


def test_cancellation_after_recording_trim_is_checked_before_model_request():
    active, sent = [True], []

    def record(**_):
        active[0] = False
        return True

    with configured_harness(
        model_settings(context_max_chars=4000),
        transport=httpx.MockTransport(lambda req: sent.append(req)),
    ) as harness:
        with pytest.raises(ExecutionStopped):
            harness.execute(
                "task",
                lambda _: True,
                context=history(),
                on_context=record,
                checkpoint=lambda: active[0],
            )
    assert sent == []


def seeded_thread(repository, scope, gateway):
    conversations = ConversationRepository(repository.engine, repository.clock)
    config = ExecutionSnapshot(model=gateway.snapshot())
    agent = conversations.create_agent(scope, "Budget test", config)
    thread = conversations.create_thread(scope, agent["agent_id"], "History")
    first = repository.create(
        scope, "private-old-prompt" + "旧" * 2000, thread_id=thread["thread_id"]
    )
    claim = repository.claim_next()
    assert claim.run.run_id == first.run_id
    repository.finish(claim, result="private-old-reply" + "答" * 2000)
    return conversations, thread


def test_worker_persists_private_trim_audit_without_rewriting_history(
    repository, scope, client, headers, clock
):
    gateway = model_settings(context_max_chars=4000)
    conversations, thread = seeded_thread(repository, scope, gateway)
    run = repository.create(scope, "next", thread_id=thread["thread_id"])
    captured = []
    original = repository.get(scope, run.run_id)
    old_claim = repository.claim_next()
    # Exercise reclaim: the retry must re-use the captured history and budget.
    clock.advance(61)
    with configured_harness(
        gateway,
        transport=httpx.MockTransport(
            lambda req: (
                captured.append(json.loads(req.content))
                or httpx.Response(200, json=completion("done"))
            )
        ),
    ) as harness:
        assert Worker(repository, harness).run_once()
    final = repository.get(scope, run.run_id)
    assert final.status == RunStatus.SUCCEEDED and final.attempt == 2
    assert final.context == original.context
    assert final.execution_config == original.execution_config
    events = repository.list_events(scope, run.run_id)
    trim = next(e for e in events if e.event_type == "context_trimmed")
    assert trim.data["attempt"] == 2
    assert trim.data["dropped_turns"] == 1 and trim.data["retained_turns"] == 0
    assert "private-old" not in json.dumps(trim.data)
    assert not repository.record_context(
        old_claim, **{k: v for k, v in trim.data.items() if k != "attempt"}
    )
    assert len(conversations.list_messages(scope, thread["thread_id"])) == 4
    body = client.get(
        f"/api/v1/runs/{run.run_id}/events/stream",
        headers={**headers, "Last-Event-ID": str(trim.sequence - 1)},
    ).text
    assert "event: context_trimmed" in body and "private-old" not in body
    with pytest.raises(RunNotFound):
        repository.list_events(replace(scope, user_id="stranger"), run.run_id)


@pytest.mark.parametrize("reason", ["cancel", "expired", "revoked"])
def test_trim_event_obeys_current_execution_ownership(repository, scope, clock, people, reason):
    from agentforge_storage.repository import RunRepository

    store, members, _ = people
    repo = (
        RunRepository(repository.engine, clock, auth_mode="database")
        if reason == "revoked"
        else repository
    )
    owner = members["alice"] if reason == "revoked" else scope
    config = ExecutionSnapshot(
        authorization_mode="database" if reason == "revoked" else "development"
    )
    run = repo.create(owner, "task", execution_config=config)
    claim = repo.claim_next()
    if reason == "cancel":
        repo.cancel(owner, run.run_id)
    elif reason == "expired":
        clock.advance(61)
    else:
        store.set_member(scope, "alice", display_name="Alice", role="viewer")
    assert not repo.record_context(
        claim,
        step=1,
        budget_chars=4000,
        before_chars=6000,
        after_chars=3000,
        dropped_turns=1,
        retained_turns=1,
    )
    assert not any(e.event_type == "context_trimmed" for e in repo.list_events(owner, run.run_id))


def test_tool_permission_checks_still_apply_after_history_trim():
    with configured_harness(
        model_settings(context_max_chars=4000),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion(None, [tool_call()]))
        ),
    ) as harness:
        with pytest.raises(RuntimeFailure, match="tool_not_allowed"):
            harness.execute(
                "task", lambda _: True, context=history(), tool_executor=ToolExecutor(())
            )


def test_explicit_skill_catalog_and_reader_survive_history_trimming():
    from agentforge_runtime.skill_tools import SKILL_READ, with_skill_reader
    from agentforge_runtime.tools import BUILTIN_TOOLS, DEFAULT_TOOLS
    from agentforge_skills.builtin import BUILTIN_SKILLS

    gateway = model_settings(context_max_chars=4000)
    skill = BUILTIN_SKILLS[0]
    snapshot = ExecutionSnapshot(
        model=gateway.snapshot(), skills=(skill,), requested_skill=skill.reference
    )
    executor = ToolExecutor(
        (*snapshot.tools, SKILL_READ),
        registry=with_skill_reader(BUILTIN_TOOLS, snapshot.skills),
        allowed=(*DEFAULT_TOOLS, SKILL_READ),
    )
    requests = []

    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    None,
                    [
                        tool_call(
                            name="skill_read", arguments=json.dumps({"reference": skill.reference})
                        )
                    ],
                ),
            )
        return httpx.Response(200, json=completion("done"))

    with configured_harness(gateway, transport=httpx.MockTransport(serve)) as harness:
        assert (
            harness.execute(
                "summarize",
                lambda _: True,
                execution_config=snapshot,
                context=history(),
                tool_executor=executor,
            )
            == "done"
        )
    catalog = next(
        json.loads(m["content"])
        for m in requests[0]["messages"]
        if m["role"] == "user" and m["content"].startswith('{"requested_skill_reference"')
    )
    assert catalog["requested_skill_reference"] == skill.reference
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert skill.manifest.content in json.loads(requests[1]["messages"][-1]["content"])["content"]
    assert all(context_size(body["messages"], body["tools"]) <= 4000 for body in requests)

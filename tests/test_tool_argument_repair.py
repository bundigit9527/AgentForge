import json

import httpx
import pytest
import sqlalchemy as sa
from agentforge_core.execution import ExecutionSnapshot, ModelConfig
from agentforge_runtime.chat_harness import configured_harness
from agentforge_storage.approvals import ApprovalRepository
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.rakazo import RakazoRepository
from agentforge_storage.schema import external_effects, tool_calls
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_approval_resume import card_for


def worker_for(repository, settings, respond):
    return Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            settings.for_snapshot(snapshot.model), transport=httpx.MockTransport(respond)
        ),
    )


def test_invalid_batch_executes_nothing_until_corrected(repository, scope):
    settings = model_settings(max_tool_repairs=2)
    run = repository.create(
        scope, "report", execution_config=ExecutionSnapshot(model=settings.snapshot())
    )
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200, json=completion(None, [tool_call("bad", arguments="{}"), tool_call("skipped")])
            )
        if len(requests) == 2:
            with repository.engine.connect() as conn:
                assert (
                    conn.execute(sa.select(sa.func.count()).select_from(tool_calls)).scalar_one()
                    == 0
                )
            results = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
            assert [r["error"] for r in results] == ["tool_invalid_arguments", "batch_not_executed"]
            assert all(r["executed"] is False for r in results)
            return httpx.Response(200, json=completion(None, [tool_call("fixed")]))
        return httpx.Response(200, json=completion())

    assert worker_for(repository, settings, respond).run_once()
    assert repository.get(scope, run.run_id).status == "succeeded"
    with repository.engine.connect() as conn:
        assert conn.execute(sa.select(tool_calls.c.call_id)).scalars().all() == ["fixed"]


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_correction_is_bounded_and_old_snapshots_reject(repository, scope, limit):
    settings = model_settings(max_tool_repairs=limit)
    run = repository.create(
        scope, "report", execution_config=ExecutionSnapshot(model=settings.snapshot())
    )
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(
            200, json=completion(None, [tool_call(str(len(calls)), arguments="{}")])
        )

    worker_for(repository, settings, respond).run_once()
    assert repository.get(scope, run.run_id).error_code == "tool_invalid_arguments"
    assert len(calls) == limit + 1
    assert ModelConfig().max_tool_repairs == 0


def test_unauthorized_tool_in_invalid_batch_is_never_repaired(repository, scope):
    settings = model_settings(max_tool_repairs=2)
    run = repository.create(
        scope, "report", execution_config=ExecutionSnapshot(model=settings.snapshot())
    )
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(
            200,
            json=completion(None, [tool_call("bad", arguments="{}"), tool_call("x", name="shell")]),
        )

    worker_for(repository, settings, respond).run_once()
    assert repository.get(scope, run.run_id).error_code == "tool_not_allowed"
    assert len(calls) == 1


def test_repaired_call_still_requires_exact_approval(repository, scope):
    settings = model_settings(max_tool_repairs=2)
    conversations = ConversationRepository(repository.engine, repository.clock)
    config = ExecutionSnapshot(model=settings.snapshot(), approval_tools=("example_report@1",))
    agent = conversations.create_agent(scope, "Approval", config)
    service = RakazoRepository(repository)
    tid = service.ensure_thread(scope, agent["agent_id"])
    run = repository.create(scope, "report", thread_id=tid)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=completion(None, [tool_call("bad", arguments="{}")]))
        if len(requests) == 2:
            return httpx.Response(
                200,
                json=completion(
                    None, [tool_call("fixed", arguments=json.dumps({"text": "approved text"}))]
                ),
            )
        return httpx.Response(200, json=completion())

    worker = worker_for(repository, settings, respond)
    worker.run_once()
    assert repository.get(scope, run.run_id).status == "waiting_input"
    card = card_for(repository, run)
    ApprovalRepository(repository).answer(scope, run.run_id, card["message_id"], "allow")
    worker.run_once()
    assert repository.get(scope, run.run_id).status == "succeeded"
    with repository.engine.connect() as conn:
        assert conn.execute(sa.select(external_effects.c.status)).scalars().all() == ["succeeded"]
        arguments = conn.execute(sa.select(tool_calls.c.arguments)).scalar_one()
    assert arguments == {"text": "approved text"}
    assert len(requests) == 3


def test_repair_budget_survives_approval_resume(repository, scope):
    settings = model_settings(max_tool_repairs=1)
    conversations = ConversationRepository(repository.engine, repository.clock)
    config = ExecutionSnapshot(model=settings.snapshot(), approval_tools=("example_report@1",))
    agent = conversations.create_agent(scope, "Approval budget", config)
    tid = RakazoRepository(repository).ensure_thread(scope, agent["agent_id"])
    run = repository.create(scope, "report", thread_id=tid)
    calls = []

    def respond(request):
        calls.append(1)
        arguments = json.dumps({"text": "approved"}) if len(calls) == 2 else "{}"
        return httpx.Response(
            200, json=completion(None, [tool_call(str(len(calls)), arguments=arguments)])
        )

    worker = worker_for(repository, settings, respond)
    worker.run_once()
    assert repository.get(scope, run.run_id).status == "waiting_input"
    card = card_for(repository, run)
    ApprovalRepository(repository).answer(scope, run.run_id, card["message_id"], "allow")
    worker.run_once()
    assert repository.get(scope, run.run_id).error_code == "tool_invalid_arguments"
    assert len(calls) == 3  # The pause did not replenish the one-repair budget.

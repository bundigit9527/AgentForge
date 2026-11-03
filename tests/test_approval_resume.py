import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunConflict, RunNotFound, RunStatus
from agentforge_runtime.chat_harness import configured_harness
from agentforge_storage.approvals import ApprovalRepository
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.rakazo import RakazoRepository
from agentforge_storage.schema import approval_requests, threads
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call


@pytest.fixture
def flow(repository, scope):
    settings = model_settings()
    conversations = ConversationRepository(repository.engine, repository.clock)
    config = ExecutionSnapshot(model=settings.snapshot(), approval_tools=("example_report@1",))
    agent = conversations.create_agent(scope, "Approval", config)
    # Native thread selection is deterministic and created through the same service as send.
    service = RakazoRepository(repository)
    tid = service.ensure_thread(scope, agent["agent_id"])
    run = repository.create(scope, "create report", thread_id=tid)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200, json=completion(None, [tool_call(arguments=json.dumps({"text": "original"}))])
            )
        assert body["messages"][-1]["role"] == "tool"
        return httpx.Response(200, json=completion("Done"))

    worker = Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            settings, transport=httpx.MockTransport(respond)
        ),
    )
    approvals = ApprovalRepository(repository)
    return worker, approvals, service, run, requests


def card_for(repository, run):
    with repository.engine.connect() as conn:
        return dict(
            conn.execute(
                sa.select(approval_requests).where(approval_requests.c.run_id == run.run_id)
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("answer", ["allow", "always", "deny"])
def test_pause_answer_resume_keeps_original_call(flow, repository, scope, answer):
    worker, approvals, service, run, requests = flow
    assert worker.run_once()
    assert repository.get(scope, run.run_id).status == RunStatus.WAITING_INPUT
    assert len(requests) == 1
    assert repository.list_tool_calls(scope, run.run_id) == []
    assert not worker.run_once()
    card = card_for(repository, run)
    snapshot = service.snapshot(scope, run.agent_id)
    assert snapshot["run"]["status"] == "waiting_input"
    assert snapshot["activeRuns"][0]["id"] == run.run_id
    assert snapshot["messages"][-1]["blocks"][0]["approvalEffectId"] == card["effect_id"]
    assert approvals.answer(scope, run.run_id, card["message_id"], answer) == {"ok": True}
    assert approvals.answer(scope, run.run_id, card["message_id"], answer) == {"ok": True}
    assert worker.run_once()
    assert repository.get(scope, run.run_id).result == "Done"
    assert len(requests) == 2  # no model re-plan before executing the approved call
    assert ("denied" in requests[-1]["messages"][-1]["content"]) == (answer == "deny")
    calls = repository.list_tool_calls(scope, run.run_id)
    assert len(calls) == (0 if answer == "deny" else 1)
    if calls:
        assert calls[0]["arguments"] == {"text": "original"}
    updated = next(
        m
        for m in service.snapshot(scope, run.agent_id)["messages"]
        if m["id"] == card["message_id"]
    )
    assert updated["blocks"][0]["status"] == "answered"
    assert approvals.granted(scope, "example_report@1") == (answer == "always")


def test_cancel_waiting_and_stale_answer(flow, repository, scope):
    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    repository.cancel(scope, run.run_id)
    with pytest.raises(RunConflict):
        approvals.answer(scope, run.run_id, card["message_id"], "allow")
    assert not worker.run_once()
    with repository.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(threads.c.active_run_id).where(threads.c.thread_id == run.thread_id)
            ).scalar()
            is None
        )


@pytest.mark.parametrize("field", ["user_id", "space_id", "tenant_id"])
def test_answer_scope_isolation(flow, repository, scope, field):
    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    with pytest.raises(RunNotFound):
        approvals.answer(
            replace(scope, **{field: "other"}), run.run_id, card["message_id"], "allow"
        )
    assert repository.get(scope, run.run_id).status == RunStatus.WAITING_INPUT


def test_conflicting_double_click_has_one_winner(flow, repository, scope):
    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)

    def answer(value):
        try:
            approvals.answer(scope, run.run_id, card["message_id"], value)
            return True
        except RunConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(answer, ["allow", "deny"])) == [False, True]
    assert worker.run_once()


def test_rest_decision_also_requeues(flow, repository, scope, client, headers):
    worker, _, _, run, _ = flow
    worker.run_once()
    effects = client.get(f"/api/v1/runs/{run.run_id}/effects", headers=headers).json()["items"]
    effect = effects[0]
    result = client.post(
        f"/api/v1/runs/{run.run_id}/effects/{effect['effect_id']}/decision",
        headers=headers,
        json={"decision": "approve", "request_hash": effect["request_hash"]},
    )
    assert result.status_code == 200, result.text
    assert repository.get(scope, run.run_id).status == RunStatus.QUEUED
    assert worker.run_once()


def test_native_answer_endpoint_and_stale_targets(flow, repository, scope, client, headers):
    from tests.test_orpc import rpc

    worker, _, service, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    payload = {
        "botId": run.agent_id,
        "runId": run.run_id,
        "messageId": card["message_id"],
        "answer": "allow",
    }
    assert (
        rpc(client, headers, "threads.answer", {**payload, "answer": "invalid"}).status_code == 422
    )
    from uuid import uuid4

    assert (
        rpc(client, headers, "threads.answer", {**payload, "threadId": str(uuid4())}).status_code
        == 409
    )
    for _ in range(2):
        response = rpc(client, headers, "threads.answer", payload)
        assert response.status_code == 200, response.text
        assert response.json()["json"] == {"ok": True}
    assert worker.run_once()
    events = service.events(scope, run.agent_id, 0)
    assert "run.waiting_input" in [e["type"] for e in events]
    assert any(
        e["type"] == "thread.message.updated" and e["payload"]["blocks"][0].get("answer") == "allow"
        for e in events
    )


def test_many_pauses_do_not_exhaust_crash_retries(flow, repository, scope, clock):
    _, approvals, _, run, _ = flow
    settings = model_settings()
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200, json=completion(None, [tool_call(call_id=f"c{i}") for i in range(5)])
            )
        body = json.loads(request.content)
        assert sum(m["role"] == "tool" for m in body["messages"]) == 5
        return httpx.Response(200, json=completion())

    worker = Worker(
        repository,
        harness_factory=lambda _: configured_harness(
            settings, transport=httpx.MockTransport(respond)
        ),
    )
    for _ in range(5):
        assert worker.run_once()
        assert repository.get(scope, run.run_id).status == RunStatus.WAITING_INPUT
        clock.advance(3600)
        assert not worker.run_once()
        with repository.engine.connect() as conn:
            card = (
                conn.execute(
                    sa.select(approval_requests).where(
                        approval_requests.c.run_id == run.run_id,
                        approval_requests.c.answer.is_(None),
                    )
                )
                .mappings()
                .one()
            )
        approvals.answer(scope, run.run_id, card["message_id"], "allow")
    assert worker.run_once()
    assert repository.get(scope, run.run_id).status == RunStatus.SUCCEEDED
    assert count == 2
    assert len(repository.list_tool_calls(scope, run.run_id)) == 5


class SimulatedCrash(BaseException):
    pass


def test_completed_tool_reused_after_checkpoint_write_crash(
    flow, repository, scope, clock, monkeypatch
):
    worker, approvals, _, run, requests = flow
    worker.run_once()
    card = card_for(repository, run)
    approvals.answer(scope, run.run_id, card["message_id"], "allow")
    original = ApprovalRepository.save

    def crash_after_result(self, claim, state):
        if state["index"] == 1:
            raise SimulatedCrash()
        original(self, claim, state)

    with monkeypatch.context() as patch:
        patch.setattr(ApprovalRepository, "save", crash_after_result)
        with pytest.raises(SimulatedCrash):
            worker.run_once()
    clock.advance(61)
    assert worker.run_once()
    assert repository.get(scope, run.run_id).status == RunStatus.SUCCEEDED
    assert len(repository.list_tool_calls(scope, run.run_id)) == 1
    assert len(requests) == 2


def test_unknown_tool_outcome_is_not_replayed(flow, repository, scope, clock):
    from agentforge_runtime.tools import BUILTIN_TOOLS, ToolRegistry

    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    approvals.answer(scope, run.run_id, card["message_id"], "allow")
    calls = []

    def crash(arguments):
        calls.append(arguments)
        raise SimulatedCrash()

    worker.tool_registry = ToolRegistry(
        [
            replace(d, handler=crash) if d.name == "example_report" else d
            for d in BUILTIN_TOOLS._definitions.values()
        ]
    )
    with pytest.raises(SimulatedCrash):
        worker.run_once()
    clock.advance(61)
    assert worker.run_once()
    assert repository.get(scope, run.run_id).error_code == "effect_uncertain"
    assert len(calls) == 1


def test_clear_waiting_removes_cards_and_blocks_old_answers(flow, repository, scope):
    worker, approvals, service, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    service.clear(scope, run.agent_id)
    assert service.snapshot(scope, run.agent_id)["messages"] == []
    with pytest.raises(RunConflict):
        approvals.answer(scope, run.run_id, card["message_id"], "allow")
    assert not worker.run_once()


def test_original_orpc_client_approval_and_reducer(flow, repository, client):
    from tests.test_orpc_interop import run_bridge

    run_bridge(client, repository, worker=flow[0], approval_mode=True)


def test_always_grant_can_be_revoked_and_is_user_scoped(flow, repository, scope):
    from agentforge_storage.schema import approval_grants

    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    approvals.answer(scope, run.run_id, card["message_id"], "always")
    assert approvals.granted(scope, "example_report@1")
    assert not approvals.granted(replace(scope, user_id="other"), "example_report@1")
    assert not approvals.granted(scope, "example_report@2")
    with repository.engine.begin() as conn:
        conn.execute(approval_grants.delete())
    # Revoking a future grant does not revoke the already approved exact operation.
    assert worker.run_once()
    assert repository.get(scope, run.run_id).status == RunStatus.SUCCEEDED


def test_permission_revoked_while_waiting_cannot_resume(engine, clock, scope):
    from agentforge_core.identity import PermissionDenied
    from agentforge_storage.identity import IdentityRepository
    from agentforge_storage.repository import RunRepository

    identity = IdentityRepository(engine, clock)
    identity.bootstrap(scope, tenant_name="Demo", space_name="Demo", display_name="Admin")
    owner = replace(scope, user_id="owner")
    identity.set_member(scope, owner.user_id, display_name="Owner", role="member")
    repo = RunRepository(engine, clock, auth_mode="database")
    settings = model_settings()
    config = ExecutionSnapshot(
        authorization_mode="database",
        model=settings.snapshot(),
        approval_tools=("example_report@1",),
    )
    service = ConversationRepository(engine, clock, auth_mode="database")
    agent = service.create_agent(owner, "Test", config)
    thread = service.create_thread(owner, agent["agent_id"], "test")
    run = repo.create(owner, "task", thread_id=thread["thread_id"])

    def respond(_):
        return httpx.Response(200, json=completion(None, [tool_call()]))

    worker = Worker(
        repo,
        harness_factory=lambda _: configured_harness(
            settings, transport=httpx.MockTransport(respond)
        ),
    )
    worker.run_once()
    card = card_for(repo, run)
    approvals = ApprovalRepository(repo)
    identity.set_member(scope, owner.user_id, display_name="Owner", role="viewer")
    with pytest.raises(PermissionDenied):
        approvals.answer(owner, run.run_id, card["message_id"], "allow")
    with pytest.raises(RunNotFound):
        approvals.answer(scope, run.run_id, card["message_id"], "allow")
    assert not worker.run_once()


def test_lease_loss_cannot_save_or_pause(flow, repository, scope, clock):
    _, approvals, _, run, _ = flow
    claim = repository.claim_next()
    clock.advance(61)
    with pytest.raises(RunConflict):
        approvals.save(claim, {"current": []})
    assert repository.get(scope, run.run_id).status == RunStatus.RUNNING


def test_permanent_grant_rest_revocation(flow, repository, scope, client, headers):
    worker, approvals, _, run, _ = flow
    worker.run_once()
    card = card_for(repository, run)
    approvals.answer(scope, run.run_id, card["message_id"], "always")
    assert client.get("/api/v1/approval-grants", headers=headers).json() == {
        "items": ["example_report@1"]
    }
    assert (
        client.delete("/api/v1/approval-grants/example_report@1", headers=headers).status_code
        == 200
    )
    assert not approvals.granted(scope, "example_report@1")


def test_sqlite_foreign_keys_remain_enforced_after_migration(engine):
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy.exc import IntegrityError

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO approval_requests (effect_id,run_id,message_id,blocks) "
                "VALUES ('missing','missing','missing','[]')"
            )
        )

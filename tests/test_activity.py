import json
from uuid import uuid4

import pytest
from agentforge_core.models import RunErrorCode
from agentforge_core.tools import ToolInvocation
from agentforge_storage.rakazo import RakazoRepository
from agentforge_worker.worker import Worker

from tests.test_approval_resume import card_for
from tests.test_approval_resume import flow as _flow
from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_identity import share
from tests.test_orpc import create_bot, rpc

flow = _flow
people = _people
enterprise = _enterprise


def send(client, headers, bot, text="Inspect public sources"):
    response = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": text})
    assert response.status_code == 200, response.text
    return response.json()["json"]["runId"]


def activity(client, headers, filter="active"):
    response = rpc(client, headers, "runs.list", {"filter": filter})
    assert response.status_code == 200, response.text
    return response.json()["json"]["runs"]


def snapshot(client, headers, bot):
    return rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]


def invocation(name="example_report", call_id=None):
    return ToolInvocation(
        call_id or str(uuid4()), name, 1, json.dumps({"text": "sensitive-tool-input"})
    )


def test_activity_live_completed_cancelled_filters_and_clear(client, headers, repository, clock):
    bot = create_bot(client, headers, notifyOnFinish=True)
    run = send(client, headers, bot, "  Multiple\n lines " + "x" * 150)
    rows = activity(client, headers)
    assert len(rows) == 1 and rows[0]["runId"] == run and rows[0]["status"] == "queued"
    assert rows[0]["botName"] == bot["name"] and rows[0]["notificationsEnabled"] is True
    assert len(rows[0]["promptSnippet"]) == 120 and "\n" not in rows[0]["promptSnippet"]
    assert activity(client, headers, "recent") == []
    claim = repository.claim_next()
    assert activity(client, headers)[0]["status"] == "running"
    clock.advance(2)
    assert repository.finish(claim, result="done")
    assert activity(client, headers) == []
    assert activity(client, headers, "recent")[0]["status"] == "completed"
    second = send(client, headers, bot)
    assert rpc(client, headers, "threads.stop", {"botId": bot["id"]}).is_success
    recent = activity(client, headers, "recent")
    assert {r["runId"]: r["status"] for r in recent} == {run: "completed", second: "cancelled"}
    assert rpc(client, headers, "threads.clear", {"botId": bot["id"]}).is_success
    assert activity(client, headers, "recent") == []
    assert rpc(client, headers, "runs.list", {"filter": "all"}).status_code == 422
    assert (
        rpc(client, headers, "runs.list", {"filter": "active", "userId": "forged"}).status_code
        == 422
    )


def test_activity_recent_twenty_and_native_selected_conversation(
    client, headers, repository, clock
):
    bot = create_bot(client, headers)
    ids = []
    for i in range(22):
        ids.append(send(client, headers, bot, f"message {i}"))
        Worker(repository).run_once()
        clock.advance(1)
    assert [r["runId"] for r in activity(client, headers, "recent")] == list(reversed(ids[-20:]))
    changed = rpc(
        client,
        headers,
        "agentforge.conversations.create",
        {
            "botId": bot["id"],
            "expectedThreadId": bot["threadId"],
            "requestId": str(uuid4()),
        },
    )
    assert changed.is_success
    assert activity(client, headers, "recent") == []
    restored = rpc(
        client,
        headers,
        "agentforge.conversations.select",
        {
            "botId": bot["id"],
            "expectedThreadId": changed.json()["json"]["threadId"],
            "threadId": bot["threadId"],
        },
    )
    assert restored.is_success
    assert len(activity(client, headers, "recent")) == 20


def test_activity_ownership_sharing_and_revocation(enterprise):
    client, headers, identities, scopes, _ = enterprise
    bot = create_bot(client, headers["alice"])
    alice = send(client, headers["alice"], bot)
    assert share(client, headers["alice"], "agent", bot["id"]).is_success
    assert share(client, headers["alice"], "run", alice).is_success
    assert activity(client, headers["bob"]) == []
    assert activity(client, headers["admin"]) == []
    bob = send(client, headers["bob"], bot)
    assert [r["runId"] for r in activity(client, headers["bob"])] == [bob]
    assert [r["runId"] for r in activity(client, headers["alice"])] == [alice]
    assert share(client, headers["alice"], "agent", bot["id"], "private").is_success
    assert activity(client, headers["bob"]) == []
    identities.set_member(
        scopes["admin"], "alice", display_name="Alice", role="member", active=False
    )
    assert rpc(client, headers["alice"], "runs.list", {"filter": "active"}).status_code in {
        401,
        403,
    }


def test_tool_activity_pending_completed_dedup_and_no_payload_leak(
    client, headers, repository, scope, clock
):
    bot = create_bot(client, headers)
    send(client, headers, bot)
    claim = repository.claim_next()
    request = invocation()
    ticket = repository.start_tool(claim, request)
    active = snapshot(client, headers, bot)
    pending = active["messages"][-1]
    assert pending["id"] == "progress:" + claim.run.run_id
    assert pending["blocks"] == [{"kind": "meta", "text": "工具执行中：example_report@1"}]
    before = active["cursor"]
    assert repository.record_text(claim, step=1, text="draft", reset=True)
    clock.advance(1.25)
    assert repository.complete_tool(
        claim, ticket, output={"text": "private result"}, content="private result"
    )
    current = snapshot(client, headers, bot)
    blocks = current["messages"][-1]["blocks"]
    assert blocks == [
        {"kind": "steps", "steps": [{"label": "example_report@1", "count": 1}], "durationMs": 1250},
        {"kind": "progress", "text": "draft"},
    ]
    service = RakazoRepository(repository)
    history = service.events(scope, bot["id"], before)
    assert history[-1]["type"] == "thread.message.updated"
    assert history[-1]["payload"]["blocks"] == blocks
    assert "sensitive-tool-input" not in json.dumps(history) and "private result" not in json.dumps(
        history
    )
    cursor = current["cursor"]
    assert repository.start_tool(claim, request).cached
    assert not repository.complete_tool(claim, ticket, content="duplicate")
    assert snapshot(client, headers, bot)["cursor"] == cursor
    assert repository.finish(claim, result="final answer")
    final = snapshot(client, headers, bot)
    assert final["messages"][-1]["blocks"] == [{"kind": "text", "text": "final answer"}, blocks[0]]
    assert not any(m["id"].startswith("progress:") for m in final["messages"])
    # Already committed history remains correct through a fresh repository/API read.
    assert (
        service.history(scope, bot["id"])["messages"][-1]["blocks"]
        == final["messages"][-1]["blocks"]
    )


@pytest.mark.parametrize("ending", ["failed", "cancelled", "expired"])
def test_failed_or_abandoned_calls_never_become_success_steps(
    client, headers, repository, scope, clock, ending
):
    bot = create_bot(client, headers)
    run = send(client, headers, bot)
    claim = repository.claim_next(lease_seconds=2)
    ticket = repository.start_tool(claim, invocation())
    if ending == "failed":
        assert repository.complete_tool(
            claim, ticket, error_code=RunErrorCode.TOOL_EXECUTION_FAILED
        )
        assert repository.finish(claim, failed=True, error_code=RunErrorCode.TOOL_EXECUTION_FAILED)
    elif ending == "cancelled":
        repository.cancel(scope, run)
    else:
        clock.advance(3)
        replacement = repository.claim_next()
        assert replacement.run.attempt == 2
        active = snapshot(client, headers, bot)
        assert "结果未确认" in str(active["messages"][-1]["blocks"])
        assert repository.finish(replacement, result="recovered without claiming earlier success")
    assert not repository.complete_tool(claim, ticket, output={}, content="stale")
    final = snapshot(client, headers, bot)
    blocks = final["messages"][-1]["blocks"]
    assert not any(b["kind"] == "steps" for b in blocks)
    assert any("失败" in b.get("text", "") or "结果未确认" in b.get("text", "") for b in blocks)
    assert "sensitive-tool-input" not in str(blocks)
    assert activity(client, headers) == []
    assert len(activity(client, headers, "recent")) == 1


def test_waiting_approval_is_activity_without_claiming_tool_execution(flow, client, headers, scope):
    worker, approvals, service, run, _ = flow
    assert worker.run_once()
    assert activity(client, headers)[0]["status"] == "waiting_input"
    before = service.snapshot(scope, run.agent_id)
    assert not any(b["kind"] == "steps" for m in before["messages"] for b in m["blocks"])
    card = card_for(worker.repository, run)
    approvals.answer(scope, run.run_id, card["message_id"], "allow")
    assert activity(client, headers)[0]["status"] == "queued"
    assert worker.run_once()
    assert activity(client, headers) == []
    assert activity(client, headers, "recent")[0]["status"] == "completed"

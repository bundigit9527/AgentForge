import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import sqlalchemy as sa
from agentforge_api.orpc import subscribe_events
from agentforge_storage.rakazo import RakazoRepository
from agentforge_storage.schema import agents, runs
from agentforge_worker.worker import Worker

from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_streaming import Connection

enterprise = _enterprise
people = _people


def rpc(client, headers, name, data=None):
    return client.post("/rpc/" + name.replace(".", "/"), headers=headers, json={"json": data or {}})


def create_bot(client, headers, **changes):
    response = rpc(client, headers, "bots.create", {"name": "投研助手", **changes})
    assert response.status_code == 200, response.text
    return response.json()["json"]


def test_rpc_chat_two_turns_and_shared_rest_state(client, headers, repository, scope):
    bot = create_bot(client, headers, title="研究", description="说明", instructions="整理资料")
    assert bot["title"] == "研究"
    assert client.get("/api/v1/agents/" + bot["id"], headers=headers).json()["name"] == bot["name"]
    before = rpc(client, headers, "threads.head", {"botId": bot["id"]}).json()["json"]["cursor"]
    for text in ["first", "second"]:
        result = rpc(
            client, headers, "threads.send", {"botId": bot["id"], "text": text, "clientNonce": text}
        )
        assert result.status_code == 200, result.text
        receipt = result.json()["json"]
        again = rpc(
            client, headers, "threads.send", {"botId": bot["id"], "text": text, "clientNonce": text}
        )
        assert again.json() == result.json()
        Worker(repository).run_once()
        snapshot = rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]
        assert snapshot["run"]["status"] == "completed"
        assert snapshot["run"]["id"] == receipt["runId"]
        assert snapshot["run"]["taskId"] == receipt["taskId"]
        assert snapshot["cursor"] > before
        before = snapshot["cursor"]
    assert [m["role"] for m in snapshot["messages"]] == ["user", "bot", "user", "bot"]
    assert [m["seq"] for m in snapshot["messages"]] == sorted(
        {m["seq"] for m in snapshot["messages"]}
    )
    feed = RakazoRepository(repository).events(scope, bot["id"], 0)
    assert [e["seq"] for e in feed] == list(range(1, before + 1))
    assert [e["payload"]["messageId"] for e in feed if e["type"] == "thread.message.created"] == [
        m["id"] for m in snapshot["messages"]
    ]
    assert rpc(client, headers, "bootstrap").json()["json"]["bots"][0]["id"] == bot["id"]


def test_rpc_cancel_and_unsupported_actions_are_not_successful(client, headers, repository):
    bot = create_bot(client, headers)
    rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "cancel"})
    assert rpc(client, headers, "threads.stop", {"botId": bot["id"]}).json()["json"] == {"ok": True}
    assert not Worker(repository).run_once()
    assert (
        rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]["run"]["status"]
        == "cancelled"
    )
    assert rpc(client, headers, "computer.boot", {"botId": bot["id"]}).status_code == 501
    assert (
        rpc(
            client,
            headers,
            "threads.send",
            {"botId": bot["id"], "text": "x", "artifactIds": ["unavailable"]},
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "method,path,body,status",
    [
        ("get", "/rpc/bots/create", None, 405),
        ("post", "/rpc/unknown", {"json": {}}, 501),
        ("post", "/rpc/bots/create", {"json": {"name": "x", "user_id": "forged"}}, 422),
        ("post", "/rpc/bots/list", {"json": {}, "meta": [[0, "x"]]}, 400),
        ("post", "/rpc/bots/list", {"json": {}, "maps": []}, 400),
        ("post", "/rpc/bots/list", {"json": []}, 422),
    ],
)
def test_rpc_errors_use_client_envelope(client, headers, method, path, body, status):
    response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == status
    error = response.json()["json"]
    assert error["defined"] is False and error["status"] == status and error["code"]


def test_protocol_rejects_large_malformed_or_non_json_without_echoing_secrets(client, headers):
    for body, media, status in [
        ("secret", "application/json", 400),
        ("{}", "text/plain", 415),
        ("x" * 128001, "application/json", 413),
    ]:
        result = client.post("/rpc/me", headers={**headers, "Content-Type": media}, content=body)
        assert result.status_code == status
        assert "secret" not in result.text
    assert rpc(client, {}, "me").json()["json"]["code"] == "UNAUTHORIZED"


def test_rpc_space_header_never_changes_scope(enterprise):
    client, headers, _, _, _ = enterprise
    result = rpc(client, {**headers["alice"], "x-rakazo-space-id": "other"}, "me")
    assert result.status_code == 403


def test_rpc_shared_bot_keeps_each_users_thread_private(enterprise, repository):
    client, headers, _, _, _ = enterprise
    bot = create_bot(client, headers["alice"])
    assert rpc(client, headers["bob"], "bots.get", {"botId": bot["id"]}).status_code == 404
    client.put(
        "/api/v1/sharing/agent/" + bot["id"], headers=headers["alice"], json={"visibility": "space"}
    )
    other = rpc(client, headers["bob"], "bots.get", {"botId": bot["id"]}).json()["json"]
    assert other["threadId"] != bot["threadId"]
    assert (
        rpc(client, headers["bob"], "bots.update", {"botId": bot["id"], "name": "bad"}).status_code
        == 404
    )
    assert (
        rpc(
            client, headers["viewer"], "threads.send", {"botId": bot["id"], "text": "no"}
        ).status_code
        == 403
    )
    rpc(client, headers["alice"], "threads.send", {"botId": bot["id"], "text": "Alice secret"})
    Worker(repository).run_once()
    assert (
        rpc(client, headers["bob"], "threads.get", {"botId": bot["id"]}).json()["json"]["messages"]
        == []
    )
    rpc(client, headers["bob"], "threads.send", {"botId": bot["id"], "text": "Bob secret"})
    Worker(repository).run_once()
    assert (
        "Alice secret" not in rpc(client, headers["bob"], "threads.get", {"botId": bot["id"]}).text
    )
    client.put(
        "/api/v1/sharing/agent/" + bot["id"],
        headers=headers["alice"],
        json={"visibility": "private"},
    )
    assert rpc(client, headers["bob"], "threads.head", {"botId": bot["id"]}).status_code == 404


def test_concurrent_bot_spawn_and_first_send_are_idempotent(
    client, headers, repository, scope, engine
):
    def create(_):
        return create_bot(client, headers, spawnKey="onboarding:first")

    with ThreadPoolExecutor(max_workers=4) as pool:
        bots = list(pool.map(create, range(4)))
    assert len({b["id"] for b in bots}) == 1
    result = rpc(
        client, headers, "bots.create", {"name": "different", "spawnKey": "onboarding:first"}
    )
    assert result.status_code == 409

    def send(_):
        return rpc(
            client,
            headers,
            "threads.send",
            {"botId": bots[0]["id"], "text": "same", "clientNonce": "same"},
        ).json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(send, range(4)))
    assert all(item == responses[0] for item in responses)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(agents)).scalar_one() == 1
        assert conn.execute(sa.select(sa.func.count()).select_from(runs)).scalar_one() == 1


def test_rpc_update_applies_next_run_while_rest_thread_default_stays_pinned(
    client, headers, repository, scope
):
    bot = create_bot(client, headers, instructions="old")
    changed = rpc(
        client,
        headers,
        "bots.update",
        {"botId": bot["id"], "name": "renamed", "title": "new", "instructions": "new"},
    )
    assert changed.status_code == 200
    assert changed.json()["json"]["instructions"] == "new"
    receipt = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "x"}).json()["json"]
    assert repository.get(scope, receipt["runId"]).execution_config.instructions == "new"
    assert repository.get(scope, receipt["runId"]).agent_version == 2
    Worker(repository).run_once()
    rest = client.post(
        "/api/v1/runs",
        headers=headers,
        json={"thread_id": bot["threadId"], "prompt": "REST pinned"},
    )
    assert rest.status_code == 201, rest.text
    assert repository.get(scope, rest.json()["run_id"]).execution_config.instructions == "old"


def test_history_uses_stable_backward_cursor(client, headers, repository, scope):
    bot = create_bot(client, headers)
    service = RakazoRepository(repository)
    for n in range(27):
        service.send(scope, {"botId": bot["id"], "text": str(n)})
        Worker(repository).run_once()
    newest = rpc(client, headers, "threads.messages", {"botId": bot["id"]}).json()["json"]
    oldest = rpc(
        client, headers, "threads.messages", {"botId": bot["id"], "before": newest["olderCursor"]}
    ).json()["json"]
    assert len(newest["messages"]) == 50 and len(oldest["messages"]) == 4
    assert oldest["olderCursor"] is None
    assert max(m["seq"] for m in oldest["messages"]) < min(m["seq"] for m in newest["messages"])


def test_thread_subscription_resume_and_revocation(enterprise, repository, people):
    client, headers, identities, users, _ = enterprise
    bot = create_bot(client, headers["alice"])
    for text in ("first", "second"):
        rpc(client, headers["alice"], "threads.send", {"botId": bot["id"], "text": text})
        Worker(repository).run_once()
    service = RakazoRepository(client.app.state.repository)
    token = people[2]["alice"]

    async def exercise():
        feed = subscribe_events(
            Connection(),
            service,
            users["alice"],
            bot["id"],
            2,
            lambda: identities.authenticate(token["token"]),
        )
        try:
            assert (await anext(feed)).startswith(": connected")
            assert (await anext(feed)).startswith("id: 3")
            identities.revoke_token(users["alice"], token["token_id"])
            error = await anext(feed)
            assert "event: error" in error and "UNAUTHORIZED" in error and "id:" not in error
            with pytest.raises(StopAsyncIteration):
                await anext(feed)
        finally:
            await feed.aclose()

    asyncio.run(exercise())


def test_unknown_or_ahead_subscription_cursor_rejected_before_stream(client, headers):
    bot = create_bot(client, headers)
    response = rpc(client, headers, "threads.subscribe", {"botId": bot["id"], "cursor": 9})
    assert response.status_code == 409
    response = rpc(
        client,
        {**headers, "Last-Event-ID": "bad"},
        "threads.subscribe",
        {"botId": bot["id"], "cursor": 0},
    )
    assert response.status_code == 400
    assert (
        rpc(client, headers, "threads.subscribe", {"botId": str(uuid4()), "cursor": 0}).status_code
        == 404
    )


def test_thread_event_failure_rolls_back_run_and_message(client, headers, repository, engine):
    bot = create_bot(client, headers)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO thread_events"):
            raise RuntimeError("test failure")

    sa.event.listen(engine, "before_cursor_execute", fail)
    try:
        assert (
            rpc(
                client, headers, "threads.send", {"botId": bot["id"], "text": "rollback"}
            ).status_code
            == 500
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail)
    assert (
        rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]["messages"] == []
    )
    assert not Worker(repository).run_once()


def test_concurrent_first_send_to_shared_bot_creates_one_private_thread(enterprise, engine):
    client, headers, _, _, _ = enterprise
    bot = create_bot(client, headers["alice"])
    client.put(
        "/api/v1/sharing/agent/" + bot["id"], headers=headers["alice"], json={"visibility": "space"}
    )

    def send(_):
        response = rpc(
            client,
            headers["bob"],
            "threads.send",
            {"botId": bot["id"], "text": "first", "clientNonce": "shared-first"},
        )
        assert response.status_code == 200, response.text
        return response.json()["json"]["runId"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(send, range(4)))) == 1
    with engine.connect() as conn:
        from agentforge_storage.schema import threads

        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(threads).where(threads.c.user_id == "bob")
            ).scalar_one()
            == 1
        )

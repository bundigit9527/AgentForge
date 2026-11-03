import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import sqlalchemy as sa
from agentforge_api.orpc import subscribe_events
from agentforge_core.models import RunConflict
from agentforge_storage.rakazo import RakazoRepository
from agentforge_storage.schema import threads
from agentforge_worker.worker import Worker

from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc
from tests.test_streaming import Connection

enterprise = _enterprise
people = _people


def new_conversation(client, headers, bot, current=None, **changes):
    return rpc(
        client,
        headers,
        "agentforge.conversations.create",
        {
            "botId": bot["id"],
            "expectedThreadId": current or bot["threadId"],
            "requestId": str(uuid4()),
            **changes,
        },
    )


def select_conversation(client, headers, bot, current, target):
    return rpc(
        client,
        headers,
        "agentforge.conversations.select",
        {
            "botId": bot["id"],
            "expectedThreadId": current,
            "threadId": target,
        },
    )


def test_original_settings_form_defaults_save_without_enabling_missing_features(client, headers):
    bot = create_bot(client, headers, instructions="v1")
    form = {
        "botId": bot["id"],
        "name": bot["name"],
        "title": "new title",
        "description": "v2",
        "instructions": "v2",
        "notifyOnFinish": True,
        "memoryScope": None,
        "autoSpeak": False,
        "voiceId": None,
        "modelProvider": None,
        "modelId": None,
        "thinkingLevel": None,
        "disabledBuiltinTools": [],
    }
    saved = rpc(client, headers, "bots.update", form)
    assert saved.status_code == 200, saved.text
    state = rpc(client, headers, "agentforge.conversations.list", {"botId": bot["id"]}).json()[
        "json"
    ]
    assert state["agentVersion"] == 2 and state["current"]["agentVersion"] == 1
    assert rpc(client, headers, "bots.update", form).status_code == 200
    assert (
        rpc(client, headers, "agentforge.conversations.list", {"botId": bot["id"]}).json()["json"][
            "agentVersion"
        ]
        == 2
    )
    for field, value in [
        ("autoSpeak", True),
        ("memoryScope", "shared"),
        ("voiceId", "voice"),
        ("disabledBuiltinTools", ["bash"]),
        ("thinkingLevel", "high"),
        ("name", None),
        ("teamChatAmbientEnabled", True),
    ]:
        result = rpc(
            client, headers, "bots.update", {**form, "title": "must not save", field: value}
        )
        assert result.status_code == 422, result.text
        assert (
            rpc(client, headers, "bots.get", {"botId": bot["id"]}).json()["json"]["title"]
            == "new title"
        )
    assert (
        rpc(
            client,
            headers,
            "bots.update",
            {"botId": bot["id"], "modelProvider": "arbitrary", "modelId": "model"},
        ).status_code
        == 422
    )
    assert (
        rpc(
            client,
            headers,
            "bots.update",
            {"botId": bot["id"], "name": "stale", "expectedVersion": 1},
        ).status_code
        == 409
    )


def test_versions_new_conversation_switch_history_and_stale_tab(client, headers, repository, scope):
    bot = create_bot(client, headers, instructions="v1")

    def send(text, tid):
        result = rpc(
            client,
            {**headers, "x-agentforge-thread-id": tid},
            "threads.send",
            {"botId": bot["id"], "text": text, "clientNonce": text},
        )
        assert result.status_code == 200, result.text
        run = repository.get(scope, result.json()["json"]["runId"])
        assert Worker(repository).run_once()
        return run

    assert send("old", bot["threadId"]).execution_config.instructions == "v1"
    assert (
        rpc(client, headers, "bots.update", {"botId": bot["id"], "instructions": "v2"}).status_code
        == 200
    )
    assert send("still old", bot["threadId"]).agent_version == 2
    created = new_conversation(client, headers, bot).json()["json"]
    tid = created["threadId"]
    assert tid != bot["threadId"]
    snapshot = rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]
    assert snapshot["threadId"] == tid and snapshot["messages"] == []
    for procedure, data in [
        ("threads.send", {"text": "stale"}),
        ("threads.stop", {}),
        ("threads.clear", {}),
    ]:
        assert (
            rpc(
                client,
                {**headers, "x-agentforge-thread-id": bot["threadId"]},
                procedure,
                {"botId": bot["id"], **data},
            ).status_code
            == 409
        )
    run = send("old", tid)  # Same nonce in another conversation is independent.
    assert (
        run.agent_version == 2 and run.execution_config.instructions == "v2" and run.context == ()
    )
    assert select_conversation(client, headers, bot, tid, bot["threadId"]).status_code == 200
    restored = rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]
    assert restored["threadId"] == bot["threadId"] and len(restored["messages"]) == 4
    assert send("back", bot["threadId"]).execution_config.instructions == "v2"
    assert select_conversation(client, headers, bot, tid, bot["threadId"]).status_code == 409


def test_native_clear_cancels_busy_work_and_keeps_cursor_monotonic(
    client, headers, repository, scope
):
    bot = create_bot(client, headers)
    first = rpc(
        client,
        headers,
        "threads.send",
        {"botId": bot["id"], "text": "completed", "clientNonce": "first"},
    ).json()["json"]
    Worker(repository).run_once()
    queued = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "busy"}).json()[
        "json"
    ]
    claim = None
    for running in [False, True]:
        if running:
            claim = repository.claim_next()
            assert claim is not None
        assert new_conversation(client, headers, bot).status_code == 409
        assert (
            select_conversation(client, headers, bot, bot["threadId"], bot["threadId"]).status_code
            == 409
        )
    service = RakazoRepository(repository)
    before = service.head(scope, bot["id"])["cursor"]
    assert rpc(client, headers, "threads.clear", {"botId": bot["id"]}).json()["json"] == {
        "ok": True
    }
    assert not repository.finish(claim, result="late")
    snap = service.snapshot(scope, bot["id"])
    assert snap["threadId"] == bot["threadId"] and snap["cursor"] > before
    assert snap["messages"] == [] and snap["run"] is None and snap["activeRuns"] == []
    assert service.events(scope, bot["id"], 0)[0]["type"] == "thread.cleared"
    assert len(service.events(scope, bot["id"], 0)) == 1
    assert "runId" not in service.events(scope, bot["id"], 0)[0]
    assert (
        client.get(f"/api/v1/threads/{bot['threadId']}/messages", headers=headers).json()["items"]
        == []
    )
    assert repository.get(scope, queued["runId"]).status == "cancelled"
    assert repository.get(scope, first["runId"]).result  # Run audit is not deleted.
    assert (
        rpc(
            client,
            headers,
            "threads.send",
            {"botId": bot["id"], "text": "completed", "clientNonce": "first"},
        ).status_code
        == 409
    )
    fresh = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "new"}).json()["json"]
    assert repository.get(scope, fresh["runId"]).context == ()


def test_clear_empty_conversation_and_repeated_clear(client, headers, repository, scope):
    bot = create_bot(client, headers)
    for expected in [1, 2]:
        assert rpc(client, headers, "threads.clear", {"botId": bot["id"]}).status_code == 200
        assert RakazoRepository(repository).head(scope, bot["id"])["cursor"] == expected


def test_selection_is_owner_scoped_even_for_shared_threads(enterprise):
    client, headers, _, _, _ = enterprise
    bot = create_bot(client, headers["alice"])
    client.put(
        "/api/v1/sharing/agent/" + bot["id"], headers=headers["alice"], json={"visibility": "space"}
    )
    client.put(
        "/api/v1/sharing/thread/" + bot["threadId"],
        headers=headers["alice"],
        json={"visibility": "space"},
    )
    bob = rpc(client, headers["bob"], "bots.get", {"botId": bot["id"]}).json()["json"]
    assert (
        select_conversation(
            client, headers["bob"], bob, bob["threadId"], bot["threadId"]
        ).status_code
        == 404
    )
    assert new_conversation(client, headers["viewer"], bot).status_code == 403
    created = new_conversation(client, headers["bob"], bob)
    assert created.status_code == 200
    alice = rpc(
        client, headers["alice"], "agentforge.conversations.list", {"botId": bot["id"]}
    ).json()["json"]
    assert alice["currentThreadId"] == bot["threadId"] and len(alice["items"]) == 1
    other = create_bot(client, headers["alice"])
    assert (
        select_conversation(
            client, headers["alice"], bot, bot["threadId"], other["threadId"]
        ).status_code
        == 404
    )


def test_concurrent_creation_and_switch_preconditions(client, headers, repository, scope, engine):
    bot = create_bot(client, headers)
    request_id = str(uuid4())

    def create(_):
        return new_conversation(client, headers, bot, requestId=request_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["json"]["threadId"] for r in results}) == 1
    current = results[0].json()["json"]["threadId"]
    assert (
        new_conversation(client, headers, bot, requestId=request_id, title="different").status_code
        == 409
    )
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(threads)) == 2

    def compete(_):
        return new_conversation(client, headers, bot, current)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    service = RakazoRepository(repository)
    tid = service.thread_id(scope, bot["id"])

    def send(_):
        try:
            service.send(scope, {"botId": bot["id"], "text": "race"}, expected_thread_id=tid)
            return "sent"
        except RunConflict:
            return "conflict"

    def switch(_):
        return new_conversation(client, headers, bot, tid).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(send, 0)
        switched = pool.submit(switch, 0)
        outcomes = (sent.result(), switched.result())
    assert outcomes in [("sent", 409), ("conflict", 200)]


def test_stream_detects_selected_thread_change_without_mixing_cursors(
    client, headers, repository, scope
):
    bot = create_bot(client, headers)
    service = RakazoRepository(repository)

    async def exercise():
        feed = subscribe_events(
            Connection(), service, scope, bot["id"], 0, lambda: scope, poll_interval=0.001
        )
        assert (await anext(feed)).startswith(": connected")
        assert new_conversation(client, headers, bot).status_code == 200
        frame = await anext(feed)
        assert "event: error" in frame and "CONFLICT" in frame
        await feed.aclose()

    asyncio.run(exercise())


def test_migration_preserves_old_rpc_thread_and_nonce(engine, client, headers, repository, scope):
    import hashlib
    from pathlib import Path

    from agentforge_storage.schema import runs
    from alembic import command
    from alembic.config import Config

    bot = create_bot(client, headers)
    receipt = rpc(
        client,
        headers,
        "threads.send",
        {"botId": bot["id"], "text": "legacy", "clientNonce": "legacy"},
    ).json()["json"]
    Worker(repository).run_once()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as conn:
        conn.execute(
            runs.update()
            .where(runs.c.run_id == receipt["runId"])
            .values(idempotency_key="rpc:" + hashlib.sha256(b"legacy").hexdigest())
        )
        config.attributes["connection"] = conn
        command.downgrade(config, "0007")
        command.upgrade(config, "head")
    replay = rpc(
        client,
        headers,
        "threads.send",
        {"botId": bot["id"], "text": "legacy", "clientNonce": "legacy"},
    )
    assert replay.json()["json"] == receipt
    state = rpc(client, headers, "agentforge.conversations.list", {"botId": bot["id"]}).json()[
        "json"
    ]
    assert state["currentThreadId"] == bot["threadId"] and len(state["items"]) == 1

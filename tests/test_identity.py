import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_api.streaming import stream_events
from agentforge_core.identity import AuthenticationFailed, PermissionDenied
from agentforge_core.models import RunConflict, Scope
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import (
    identity_memberships,
    identity_spaces,
    identity_tenants,
    identity_tokens,
    identity_users,
    runs,
)
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_skills import FakeHub, install_body
from tests.test_streaming import Connection
from tests.test_tools import fake_registry


@pytest.fixture
def people(engine, clock, scope):
    store = IdentityRepository(engine, clock)
    store.bootstrap(scope, tenant_name="Test tenant", space_name="Test space", display_name="Admin")
    members = {"admin": scope}
    for name, role in (("alice", "member"), ("bob", "member"), ("viewer", "viewer")):
        members[name] = replace(scope, user_id=name)
        store.set_member(scope, name, display_name=name, role=role)
    credentials = {name: store.issue_token(who) for name, who in members.items()}
    return store, members, credentials


@pytest.fixture
def enterprise(settings, repository, people):
    store, scopes, credentials = people
    hub = FakeHub()
    with ExitStack() as stack:
        client = stack.enter_context(
            TestClient(
                create_app(
                    replace(settings, auth_mode="database"),
                    repository,
                    model_settings=ModelSettings(),
                    skill_hub=hub,
                )
            )
        )
        headers = {
            name: {"Authorization": "Bearer " + c["token"]} for name, c in credentials.items()
        }
        yield client, headers, store, scopes, hub


def make_agent(client, headers, **fields):
    response = client.post(
        "/api/v1/agents", headers=headers, json={"name": "Private agent", **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_run(client, headers, agent=None):
    agent = agent or make_agent(client, headers)
    thread = client.post("/api/v1/threads", headers=headers, json={"agent_id": agent["agent_id"]})
    assert thread.status_code == 201, thread.text
    thread = thread.json()
    run = client.post(
        "/api/v1/runs",
        headers=headers,
        json={"thread_id": thread["thread_id"], "prompt": "private input"},
    )
    assert run.status_code == 201, run.text
    return thread, run.json()


def share(client, headers, kind, identifier, visibility="space"):
    return client.put(
        f"/api/v1/sharing/{kind}/{identifier}", headers=headers, json={"visibility": visibility}
    )


def test_database_tokens_are_hashed_scoped_and_never_returned_in_lists(enterprise, engine):
    client, headers, identities, scopes, _ = enterprise
    alice = client.get("/api/v1/me", headers=headers["alice"]).json()
    assert alice == {
        "tenant_id": scopes["alice"].tenant_id,
        "space_id": scopes["alice"].space_id,
        "user_id": "alice",
        "role": "member",
        "auth_mode": "database",
    }
    raw = headers["alice"]["Authorization"].split(" ")[1]
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(identity_tokens).where(identity_tokens.c.user_id == "alice")
            )
            .mappings()
            .one()
        )
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in str(dict(row))
    listing = client.get("/api/v1/auth/tokens", headers=headers["alice"])
    assert listing.status_code == 200 and len(listing.json()["items"]) == 1
    assert raw not in listing.text and "token_hash" not in listing.text
    forged = {**headers["alice"], "X-User-Id": "user-a", "X-Tenant-Id": "other", "X-Role": "admin"}
    assert client.get("/api/v1/me", headers=forged).json() == alice


def test_invalid_expired_revoked_tokens_and_development_fallback_are_rejected(
    enterprise,
    people,
    clock,
    settings,
):
    client, headers, identities, scopes, _ = enterprise
    for token in ("af_" + "x" * 43, "invalid", settings.dev_token):
        assert (
            client.get("/api/v1/me", headers={"Authorization": "Bearer " + token}).status_code
            == 401
        )
    assert client.get("/api/v1/me").status_code == 401
    short = identities.issue_token(scopes["alice"], lifetime_seconds=60)
    clock.advance(61)
    assert (
        client.get("/api/v1/me", headers={"Authorization": "Bearer " + short["token"]}).status_code
        == 401
    )
    token_id = people[2]["alice"]["token_id"]
    assert (
        client.delete(f"/api/v1/auth/tokens/{token_id}", headers=headers["bob"]).status_code == 404
    )
    assert (
        client.delete(f"/api/v1/auth/tokens/{token_id}", headers=headers["alice"]).status_code
        == 204
    )
    assert client.get("/api/v1/me", headers=headers["alice"]).status_code == 401


@pytest.mark.parametrize("lifetime", [0, -1, float("nan"), float("inf"), 86400 * 366])
def test_invalid_token_lifetime_is_rejected(people, lifetime):
    identities, scopes, _ = people
    with pytest.raises(ValueError):
        identities.issue_token(scopes["alice"], lifetime_seconds=lifetime)


def test_private_assets_remain_private_even_from_space_admin(enterprise, repository):
    client, headers, _, _, _ = enterprise
    agent = make_agent(client, headers["alice"])
    thread, run = make_run(client, headers["alice"], agent)
    Worker(repository).run_once()
    for who in ("bob", "admin"):
        for path in (
            f"agents/{agent['agent_id']}",
            f"threads/{thread['thread_id']}",
            f"threads/{thread['thread_id']}/messages",
            f"runs/{run['run_id']}",
            f"runs/{run['run_id']}/events",
            f"runs/{run['run_id']}/attempts",
            f"runs/{run['run_id']}/tool-calls",
            f"runs/{run['run_id']}/events/stream",
        ):
            assert client.get("/api/v1/" + path, headers=headers[who]).status_code == 404
        assert client.get("/api/v1/agents", headers=headers[who]).json()["items"] == []
        assert client.get("/api/v1/threads", headers=headers[who]).json()["items"] == []


def test_shared_agent_can_be_used_without_sharing_private_history(enterprise, repository):
    client, headers, _, scopes, _ = enterprise
    agent = make_agent(client, headers["alice"])
    old_thread, old_run = make_run(client, headers["alice"], agent)
    Worker(repository).run_once()
    assert share(client, headers["alice"], "agent", agent["agent_id"]).status_code == 200
    assert (
        client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers["bob"]).status_code == 200
    )
    assert len(client.get("/api/v1/agents", headers=headers["bob"]).json()["items"]) == 1
    assert (
        client.get(f"/api/v1/threads/{old_thread['thread_id']}", headers=headers["bob"]).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/v1/agents/{agent['agent_id']}",
            headers=headers["bob"],
            json={"name": "changed", "expected_version": 1},
        ).status_code
        == 404
    )
    assert share(client, headers["bob"], "agent", agent["agent_id"], "private").status_code == 404
    new_thread, new_run = make_run(client, headers["bob"], agent)
    Worker(repository).run_once()
    assert repository.get(scopes["bob"], new_run["run_id"]).status == "succeeded"
    assert (
        client.get(f"/api/v1/runs/{new_run['run_id']}", headers=headers["alice"]).status_code == 404
    )
    assert new_thread["thread_id"] != old_thread["thread_id"]


@pytest.mark.parametrize("kind", ["thread", "run"])
def test_shared_conversation_or_run_is_read_only_and_revocable(enterprise, repository, kind):
    client, headers, _, _, _ = enterprise
    thread, run = make_run(client, headers["alice"])
    Worker(repository).run_once()
    identifier = thread["thread_id"] if kind == "thread" else run["run_id"]
    assert share(client, headers["alice"], kind, identifier).status_code == 200
    for suffix in ("", "/events", "/attempts", "/tool-calls", "/events/stream"):
        assert (
            client.get(
                f"/api/v1/runs/{run['run_id']}{suffix}", headers=headers["viewer"]
            ).status_code
            == 200
        )
    history = client.get(f"/api/v1/threads/{thread['thread_id']}/messages", headers=headers["bob"])
    assert history.status_code == (200 if kind == "thread" else 404)
    assert (
        client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers["bob"]).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/runs",
            headers=headers["bob"],
            json={"thread_id": thread["thread_id"], "prompt": "append"},
        ).status_code
        == 404
    )
    assert share(client, headers["alice"], kind, identifier, "private").status_code == 200
    assert client.get(f"/api/v1/runs/{run['run_id']}", headers=headers["bob"]).status_code == 404


def test_shared_skill_binding_needs_permission_and_cannot_edit_owner_installation(enterprise):
    client, headers, _, _, hub = enterprise
    installed = client.post(
        "/api/v1/skills/install", headers=headers["alice"], json=install_body(hub)
    ).json()
    ref = installed["reference"]
    assert (
        client.post(
            "/api/v1/agents", headers=headers["bob"], json={"name": "uses skill", "skills": [ref]}
        ).status_code
        == 404
    )
    assert share(client, headers["alice"], "skill", ref).status_code == 200
    assert len(client.get("/api/v1/skills", headers=headers["bob"]).json()["items"]) == 4
    agent = make_agent(client, headers["bob"], skills=[ref])
    assert agent["execution_config"]["skills"][0]["reference"] == ref
    assert (
        client.delete(f"/api/v1/skills/installations/{ref}", headers=headers["bob"]).status_code
        == 404
    )
    assert share(client, headers["alice"], "skill", ref, "private").status_code == 200
    assert len(client.get("/api/v1/skills", headers=headers["bob"]).json()["items"]) == 3
    assert (
        client.post(
            "/api/v1/threads", headers=headers["bob"], json={"agent_id": agent["agent_id"]}
        ).status_code
        == 404
    )


def test_shared_agent_does_not_disclose_a_private_dependency(enterprise):
    client, headers, _, _, hub = enterprise
    ref = client.post(
        "/api/v1/skills/install", headers=headers["alice"], json=install_body(hub)
    ).json()["reference"]
    agent = make_agent(client, headers["alice"], skills=[ref])
    share(client, headers["alice"], "agent", agent["agent_id"])
    assert (
        client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers["bob"]).status_code == 404
    )
    assert client.get("/api/v1/agents", headers=headers["bob"]).json()["items"] == []
    share(client, headers["alice"], "skill", ref)
    assert (
        client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers["bob"]).status_code == 200
    )


def test_viewer_and_member_cannot_elevate_their_role_or_write_as_viewer(enterprise):
    client, headers, _, _, hub = enterprise
    for who in ("viewer", "alice"):
        assert client.get("/api/v1/members", headers=headers[who]).status_code == 403
        assert (
            client.put(
                f"/api/v1/members/{who}",
                headers=headers[who],
                json={"display_name": who, "role": "admin"},
            ).status_code
            == 403
        )
    viewer = headers["viewer"]
    agent = make_agent(client, headers["alice"])
    share(client, headers["alice"], "agent", agent["agent_id"])
    for path, body in (
        ("agents", {"name": "new"}),
        ("threads", {"agent_id": agent["agent_id"]}),
        ("runs", {"prompt": "new"}),
        ("skills/install", install_body(hub)),
    ):
        assert client.post("/api/v1/" + path, headers=viewer, json=body).status_code == 403
    assert hub.requests == []
    assert (
        client.put(
            "/api/v1/members/bob",
            headers=headers["admin"],
            json={"display_name": "b", "role": "admin", "tenant_id": "other"},
        ).status_code
        == 422
    )


def test_member_removal_disables_token_and_queued_execution(enterprise, repository, engine):
    client, headers, _, _, _ = enterprise
    thread, run = make_run(client, headers["alice"])
    response = client.put(
        "/api/v1/members/alice",
        headers=headers["admin"],
        json={"display_name": "Alice", "role": "member", "active": False},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/me", headers=headers["alice"]).status_code == 401
    assert Worker(repository).run_once()  # Default worker still enforces a database-mode snapshot.
    with engine.connect() as connection:
        row = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run["run_id"]))
            .mappings()
            .one()
        )
    assert row["status"] == "failed" and row["error_code"] == "access_revoked"
    client.put(
        "/api/v1/members/alice",
        headers=headers["admin"],
        json={"display_name": "Alice", "role": "member"},
    )
    history = client.get(
        f"/api/v1/threads/{thread['thread_id']}/messages", headers=headers["alice"]
    ).json()["items"]
    assert len(history) == 1 and history[0]["run_status"] == "failed"


@pytest.mark.parametrize("revocation", ["agent", "skill", "role"])
def test_revocation_before_a_tool_blocks_model_results_and_future_tools(
    enterprise,
    settings,
    repository,
    revocation,
):
    client, headers, identities, users, hub = enterprise
    installed = client.post(
        "/api/v1/skills/install", headers=headers["alice"], json=install_body(hub)
    ).json()
    ref = installed["reference"]
    share(client, headers["alice"], "skill", ref)
    agent = make_agent(client, headers["alice"], skills=[ref])
    share(client, headers["alice"], "agent", agent["agent_id"])
    gateway = model_settings()
    # Update the owner's Agent to a real-protocol snapshot; the transport remains mocked.
    with TestClient(
        create_app(
            replace(settings, auth_mode="database"),
            repository,
            model_settings=gateway,
            skill_hub=hub,
        )
    ) as configured:
        updated = configured.put(
            f"/api/v1/agents/{agent['agent_id']}",
            headers=headers["alice"],
            json={"name": "updated", "skills": [ref], "expected_version": 1},
        )
        assert updated.status_code == 200
        thread, run = make_run(configured, headers["bob"], updated.json())
    calls = []

    def respond(request):
        calls.append(request)
        if revocation == "role":
            identities.set_member(users["admin"], "bob", display_name="Bob", role="viewer")
        else:
            share(
                client,
                headers["alice"],
                revocation,
                ref if revocation == "skill" else agent["agent_id"],
                "private",
            )
        return httpx.Response(200, json=completion(None, calls=[tool_call()]))

    Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            gateway.for_snapshot(snapshot.model),
            transport=httpx.MockTransport(respond),
        ),
    ).run_once()
    current = repository.get(users["bob"], run["run_id"])
    assert current.status == "failed" and current.error_code == "access_revoked"
    assert repository.list_tool_calls(users["bob"], run["run_id"]) == []
    assert len(calls) == 1


def test_revocation_during_inflight_tool_discards_its_result(enterprise, repository):
    client, headers, identities, users, _ = enterprise
    _, run = make_run(client, headers["alice"])

    def handler(arguments):
        identities.set_member(users["admin"], "alice", display_name="Alice", role="viewer")
        return {"text": "must not publish"}

    Worker(
        repository, tool_registry=fake_registry(handler), allowed_tools=("example_report@1",)
    ).run_once()
    current = repository.get(users["alice"], run["run_id"])
    assert current.error_code == "access_revoked" and current.result is None
    calls = repository.list_tool_calls(users["alice"], run["run_id"])
    assert calls[0]["status"] == "abandoned" and calls[0]["content"] is None


def test_stream_checks_token_revocation_while_draining_an_existing_batch(
    enterprise, repository, people
):
    client, headers, identities, users, _ = enterprise
    _, run = make_run(client, headers["alice"])
    repository.claim_next()
    repo = RunRepository(repository.engine, repository.clock, auth_mode="database")
    token = people[2]["alice"]

    async def exercise():
        stream = stream_events(
            Connection(),
            repo,
            users["alice"],
            run["run_id"],
            0,
            authorize=lambda: identities.authenticate(token["token"]) == users["alice"],
        )
        try:
            assert (await anext(stream)).startswith("id: 1")
            identities.revoke_token(users["alice"], token["token_id"])
            remaining = [item async for item in stream]
            assert remaining == ['event: stream_error\ndata: {"code":"access_unavailable"}\n\n']
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_last_admin_guard_is_serialized(enterprise, people, engine):
    _, _, identities, users, _ = enterprise
    admin = users["admin"]
    with pytest.raises(RunConflict):
        identities.set_member(admin, admin.user_id, display_name="Admin", role="viewer")
    identities.set_member(admin, "bob", display_name="Bob", role="admin")

    def demote(actor):
        try:
            identities.set_member(actor, actor.user_id, display_name="Former admin", role="member")
            return True
        except (RunConflict, PermissionDenied):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(demote, [admin, users["bob"]])) == 1
    with engine.connect() as connection:
        count = connection.execute(
            sa.select(sa.func.count())
            .select_from(identity_memberships)
            .where(
                identity_memberships.c.role == "admin",
                identity_memberships.c.active.is_(True),
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    "other", [Scope("other-tenant", "space-a", "alice"), Scope("tenant-a", "other-space", "alice")]
)
def test_space_sharing_does_not_cross_tenant_or_space(enterprise, other):
    client, headers, identities, _, _ = enterprise
    identities.bootstrap(other, tenant_name="Other", space_name="Other", display_name="Other")
    external = {"Authorization": "Bearer " + identities.issue_token(other)["token"]}
    agent = make_agent(client, headers["alice"])
    share(client, headers["alice"], "agent", agent["agent_id"])
    assert client.get(f"/api/v1/agents/{agent['agent_id']}", headers=external).status_code == 404
    assert client.get("/api/v1/agents", headers=external).json()["items"] == []
    assert share(client, external, "agent", agent["agent_id"]).status_code == 404


def test_bootstrap_does_not_overwrite_existing_membership(people):
    identities, users, _ = people
    with pytest.raises(RunConflict):
        identities.bootstrap(
            users["bob"], tenant_name="Other", space_name="Other", display_name="Other"
        )
    assert (
        next(m for m in identities.list_members(users["admin"]) if m["user_id"] == "bob")["role"]
        == "member"
    )


def test_database_worker_does_not_trust_legacy_development_scope(repository, scope):
    legacy = repository.create(scope, "legacy")
    secured = RunRepository(repository.engine, repository.clock, auth_mode="database")
    Worker(secured).run_once()
    assert repository.get(scope, legacy.run_id).error_code == "access_revoked"


def test_external_identity_adapter_still_requires_local_membership(settings, repository, people):
    identities, users, _ = people

    class Adapter:
        scope = users["alice"]

        def authenticate(self, token):
            if token != "verified-test-credential":
                raise AuthenticationFailed()
            return self.scope

    adapter = Adapter()
    with TestClient(
        create_app(
            replace(settings, auth_mode="database"),
            repository,
            model_settings=ModelSettings(),
            identity_provider=adapter,
        )
    ) as client:
        header = {"Authorization": "Bearer verified-test-credential"}
        assert client.get("/api/v1/me", headers=header).json()["role"] == "member"
        adapter.scope = replace(users["alice"], space_id="not-a-member")
        assert client.get("/api/v1/me", headers=header).status_code == 403


def test_development_token_cannot_manage_matching_database_admin(client, headers, people):
    assert client.get("/api/v1/me", headers=headers).json()["role"] == "member"
    assert client.get("/api/v1/members", headers=headers).status_code == 403
    assert (
        client.put(
            "/api/v1/members/new", headers=headers, json={"display_name": "New", "role": "admin"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/auth/tokens", headers=headers).status_code == 403
    token_id = people[2]["admin"]["token_id"]
    assert client.delete(f"/api/v1/auth/tokens/{token_id}", headers=headers).status_code == 403


@pytest.mark.parametrize("level", ["tenant", "space", "user"])
def test_inactive_identity_hierarchy_blocks_tokens_and_execution(
    enterprise, engine, repository, level
):
    client, headers, _, users, _ = enterprise
    _, run = make_run(client, headers["alice"])
    table = {"tenant": identity_tenants, "space": identity_spaces, "user": identity_users}[level]
    with engine.begin() as connection:
        where = table.c.tenant_id == users["alice"].tenant_id
        if level == "user":
            where &= table.c.user_id == "alice"
        connection.execute(table.update().where(where).values(active=False))
    assert client.get("/api/v1/me", headers=headers["alice"]).status_code == 401
    Worker(repository).run_once()
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(runs.c.error_code).where(runs.c.run_id == run["run_id"])
            ).scalar_one()
            == "access_revoked"
        )


def test_local_operator_cli_bootstrap_and_one_time_token(engine, database_url, monkeypatch, capsys):
    from agentforge_api.admin import main

    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("AGENTFORGE_AUTH_MODE", "database")
    base = [
        "agentforge-admin",
        "bootstrap",
        "--tenant",
        "cli",
        "--space",
        "cli",
        "--user",
        "operator",
    ]
    monkeypatch.setattr(
        "sys.argv",
        [*base, "--tenant-name", "CLI", "--space-name", "CLI", "--display-name", "Operator"],
    )
    main()
    assert "created" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", [base[0], "issue-token", *base[2:], "--hours", "1"])
    main()
    credential = json.loads(capsys.readouterr().out)
    identities = IdentityRepository(engine)
    assert identities.authenticate(credential["token"]) == Scope("cli", "cli", "operator")
    assert credential["token"] not in str(identities.list_tokens(Scope("cli", "cli", "operator")))


def test_admin_can_revoke_space_token_but_cannot_delete_last_admin(enterprise, people):
    client, headers, _, users, _ = enterprise
    identifier = people[2]["bob"]["token_id"]
    assert (
        client.delete(f"/api/v1/auth/tokens/{identifier}", headers=headers["admin"]).status_code
        == 204
    )
    assert client.get("/api/v1/me", headers=headers["bob"]).status_code == 401
    assert (
        client.put(
            f"/api/v1/members/{users['admin'].user_id}",
            headers=headers["admin"],
            json={"display_name": "Admin", "role": "admin", "active": False},
        ).status_code
        == 409
    )


def test_agent_pagination_counts_only_agents_with_readable_dependencies(enterprise, clock):
    client, headers, _, _, hub = enterprise
    ref = client.post(
        "/api/v1/skills/install", headers=headers["alice"], json=install_body(hub)
    ).json()["reference"]
    hidden = make_agent(client, headers["alice"], skills=[ref])
    share(client, headers["alice"], "agent", hidden["agent_id"])
    expected = []
    for _ in range(2):
        clock.advance(1)
        item = make_agent(client, headers["alice"])
        expected.append(item["agent_id"])
        share(client, headers["alice"], "agent", item["agent_id"])
    for offset, identifier in enumerate(expected):
        items = client.get(
            f"/api/v1/agents?offset={offset}&limit=1", headers=headers["bob"]
        ).json()["items"]
        assert [item["agent_id"] for item in items] == [identifier]


def test_inactive_user_is_not_counted_as_an_available_administrator(people, engine):
    identities, users, _ = people
    identities.set_member(users["admin"], "bob", display_name="Bob", role="admin")
    with engine.begin() as connection:
        connection.execute(
            identity_users.update().where(identity_users.c.user_id == "bob").values(active=False)
        )
    with pytest.raises(RunConflict):
        identities.set_member(
            users["admin"], users["admin"].user_id, display_name="Admin", role="member"
        )

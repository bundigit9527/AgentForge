from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import sqlalchemy as sa
from agentforge_core.effects import EffectRequest
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_storage.effects import ExternalEffectRepository
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import identity_memberships


@pytest.fixture
def effect_setup(repository, scope):
    run = repository.create(scope, "effect test", execution_config=ExecutionSnapshot())
    claim = repository.claim_next(lease_seconds=60)
    ledger = ExternalEffectRepository(repository)
    request = EffectRequest(
        tool_reference="demo_write@1", target="demo:account", arguments={"x": 1}
    )
    effect = ledger.request(claim, "step:1", request)
    return ledger, run, claim, request, effect


def approve(ledger, scope, run, effect):
    return ledger.decide(
        scope,
        run.run_id,
        effect["effect_id"],
        decision="approve",
        request_hash=effect["request_hash"],
    )


def test_review_dispatch_and_result_are_durable_and_idempotent(effect_setup, repository, scope):
    ledger, run, claim, request, effect = effect_setup
    assert ledger.request(claim, "step:1", request) == effect
    with pytest.raises(RunConflict):
        ledger.begin_dispatch(claim, effect["effect_id"])
    assert approve(ledger, scope, run, effect)["status"] == "approved"
    approve(ledger, scope, run, effect)
    assert ledger.begin_dispatch(claim, effect["effect_id"])["status"] == "dispatched"
    for _ in range(2):
        outcome = ledger.complete(claim, effect["effect_id"], succeeded=True, result={"id": "demo"})
        assert outcome["status"] == "succeeded"
    with pytest.raises(RunConflict):
        ledger.complete(claim, effect["effect_id"], succeeded=True, result={"id": "changed"})
    assert ExternalEffectRepository(repository).list(scope, run.run_id) == [outcome]
    events = [
        e.data
        for e in repository.list_events(scope, run.run_id)
        if e.event_type == "effect_updated"
    ]
    assert [e["status"] for e in events] == ["pending", "approved", "dispatched", "succeeded"]
    assert all(set(e) == {"effect_id", "status"} for e in events)


def test_changed_request_or_review_digest_cannot_reuse_approval(effect_setup, scope):
    ledger, run, claim, request, effect = effect_setup
    with pytest.raises(RunConflict):
        ledger.request(claim, "step:1", request.model_copy(update={"arguments": {"x": 2}}))
    with pytest.raises(RunConflict):
        ledger.decide(
            scope, run.run_id, effect["effect_id"], decision="approve", request_hash="0" * 64
        )
    assert ledger.list(scope, run.run_id)[0]["status"] == "pending"


def test_rejection_is_final(effect_setup, scope):
    ledger, run, claim, _, effect = effect_setup
    for _ in range(2):
        ledger.decide(
            scope,
            run.run_id,
            effect["effect_id"],
            decision="reject",
            request_hash=effect["request_hash"],
        )
    with pytest.raises(RunConflict):
        approve(ledger, scope, run, effect)
    with pytest.raises(RunConflict):
        ledger.begin_dispatch(claim, effect["effect_id"])


@pytest.mark.parametrize("field", ["tenant_id", "space_id", "user_id"])
def test_approval_and_reads_are_owner_scoped(effect_setup, scope, field):
    ledger, run, _, _, effect = effect_setup
    other = replace(scope, **{field: "someone-else"})
    with pytest.raises(RunNotFound):
        ledger.list(other, run.run_id)
    with pytest.raises(RunNotFound):
        approve(ledger, other, run, effect)


@pytest.mark.parametrize("action", ["request", "dispatch", "complete"])
def test_expired_claim_cannot_mutate_ledger(effect_setup, scope, clock, action):
    ledger, run, claim, request, effect = effect_setup
    approve(ledger, scope, run, effect)
    if action == "complete":
        ledger.begin_dispatch(claim, effect["effect_id"])
    clock.advance(61)
    with pytest.raises(RunConflict):
        if action == "request":
            ledger.request(claim, "step:2", request)
        elif action == "dispatch":
            ledger.begin_dispatch(claim, effect["effect_id"])
        else:
            ledger.complete(claim, effect["effect_id"], succeeded=True, result={})


def test_crash_after_dispatch_never_automatically_replays(effect_setup, scope, repository, clock):
    ledger, run, claim, request, effect = effect_setup
    approve(ledger, scope, run, effect)
    ledger.begin_dispatch(claim, effect["effect_id"])
    clock.advance(61)
    new_claim = repository.claim_next()
    assert new_claim.attempt_id != claim.attempt_id
    recovered = ledger.request(new_claim, "step:1", request)
    assert recovered["status"] == "dispatched"
    with pytest.raises(RunConflict):
        ledger.begin_dispatch(new_claim, effect["effect_id"])
    with pytest.raises(RunConflict):
        ledger.complete(new_claim, effect["effect_id"], succeeded=True, result={})


def test_concurrent_dispatch_has_one_winner(effect_setup, scope):
    ledger, run, claim, _, effect = effect_setup
    approve(ledger, scope, run, effect)

    def dispatch(_):
        try:
            ledger.begin_dispatch(claim, effect["effect_id"])
            return True
        except RunConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(dispatch, range(2))) == [False, True]


def test_cancel_blocks_approval_and_dispatch(effect_setup, scope, repository):
    ledger, run, claim, _, effect = effect_setup
    repository.cancel(scope, run.run_id)
    with pytest.raises(RunConflict):
        approve(ledger, scope, run, effect)
    with pytest.raises(RunConflict):
        ledger.begin_dispatch(claim, effect["effect_id"])
    assert ledger.list(scope, run.run_id)[0]["status"] == "pending"


def test_rest_review_and_auth(effect_setup, client, headers):
    _, run, _, _, effect = effect_setup
    path = f"/api/v1/runs/{run.run_id}/effects"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers).json()["items"] == [effect]
    decision = path + f"/{effect['effect_id']}/decision"
    assert client.post(decision, headers=headers, json={"decision": "approve"}).status_code == 422
    assert (
        client.post(
            decision,
            headers=headers,
            json={"decision": "approve", "request_hash": effect["request_hash"]},
        ).json()["status"]
        == "approved"
    )
    assert client.get(path + "?limit=101", headers=headers).status_code == 422


def test_membership_revocation_and_viewer_cannot_approve(engine, clock, scope):
    identity = IdentityRepository(engine, clock)
    identity.bootstrap(scope, tenant_name="Demo", space_name="Demo", display_name="Admin")
    member = replace(scope, user_id="member")
    identity.set_member(scope, member.user_id, display_name="Member", role="member")
    repo = RunRepository(engine, clock, auth_mode="database")
    run = repo.create(
        member, "test", execution_config=ExecutionSnapshot(authorization_mode="database")
    )
    claim = repo.claim_next()
    ledger = ExternalEffectRepository(repo)
    effect = ledger.request(
        claim, "op", EffectRequest(tool_reference="demo@1", target="demo", arguments={})
    )
    identity.set_member(scope, member.user_id, display_name="Member", role="viewer")
    with pytest.raises(PermissionDenied):
        approve(ledger, member, run, effect)
    with pytest.raises(RunNotFound):
        approve(ledger, scope, run, effect)
    with engine.begin() as connection:
        connection.execute(
            identity_memberships.update()
            .where(identity_memberships.c.user_id == member.user_id)
            .values(active=False)
        )
    with pytest.raises(RunConflict):
        ledger.request(
            claim, "op", EffectRequest(tool_reference="demo@1", target="demo", arguments={})
        )


def test_schema_matches_migration(engine):
    from agentforge_storage.schema import metadata
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


@pytest.mark.parametrize("arguments", [{"v": float("nan")}, {"v": "x" * 20001}])
def test_request_is_bounded(arguments):
    with pytest.raises(ValueError):
        EffectRequest(tool_reference="demo@1", target="demo", arguments=arguments)


def test_failure_does_not_grant_retry(effect_setup, scope):
    ledger, run, claim, _, effect = effect_setup
    approve(ledger, scope, run, effect)
    ledger.begin_dispatch(claim, effect["effect_id"])
    ledger.complete(claim, effect["effect_id"], succeeded=False, result={"code": "remote_rejected"})
    with pytest.raises(RunConflict):
        ledger.begin_dispatch(claim, effect["effect_id"])


def test_effect_cannot_move_to_another_run(effect_setup, repository, scope):
    ledger, _, _, _, effect = effect_setup
    other = repository.create(scope, "other", execution_config=ExecutionSnapshot())
    with pytest.raises(RunNotFound):
        approve(ledger, scope, other, effect)


def test_concurrent_conflicting_reviews_have_one_winner(effect_setup, scope):
    ledger, run, _, _, effect = effect_setup

    def decide(decision):
        try:
            ledger.decide(
                scope,
                run.run_id,
                effect["effect_id"],
                decision=decision,
                request_hash=effect["request_hash"],
            )
            return True
        except RunConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(decide, ["approve", "reject"])) == [False, True]


def test_migration_preserves_existing_run_and_events(effect_setup, repository, engine, scope):
    from pathlib import Path

    from agentforge_storage.schema import SCHEMA_REVISION
    from alembic import command
    from alembic.config import Config

    _, run, _, _, _ = effect_setup
    before = repository.list_events(scope, run.run_id)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0011")
        command.upgrade(config, "head")
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            == SCHEMA_REVISION
        )
    assert repository.get(scope, run.run_id).prompt == run.prompt
    assert repository.list_events(scope, run.run_id) == before

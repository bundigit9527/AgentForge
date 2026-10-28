from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa
from agentforge_core.models import RunStatus
from agentforge_runtime.harness import Harness, ToolCall
from agentforge_storage.schema import events
from agentforge_worker.lease import LeaseHeartbeat
from agentforge_worker.worker import Worker
from sqlalchemy.exc import OperationalError


def test_two_workers_cannot_claim_same_attempt(repository, scope):
    repository.create(scope, "one task")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: repository.claim_next(), range(2)))
    assert sum(claim is not None for claim in claims) == 1


def test_expired_attempt_is_fenced_after_worker_recovery(repository, scope, clock):
    run = repository.create(scope, "recover")
    old = repository.claim_next()
    assert repository.claim_next() is None
    clock.advance(61)
    new = repository.claim_next()
    assert new.attempt_id != old.attempt_id
    assert new.run.attempt == 2
    assert not repository.record_tool(old, "example_report")
    assert not repository.finish(old, result="stale result")
    assert repository.finish(new, result="recovered result")
    assert repository.get(scope, run.run_id).result == "recovered result"


def test_expired_lease_cannot_complete_before_reclaim(repository, scope, clock):
    repository.create(scope, "expired")
    claim = repository.claim_next()
    clock.advance(61)
    assert not repository.finish(claim, result="too late")


def test_running_cancel_prevents_tool_and_late_result(repository, scope):
    run = repository.create(scope, "cancel during execution")
    claim = repository.claim_next()
    repository.cancel(scope, run.run_id)
    assert not repository.record_tool(claim, "example_report")
    assert not repository.finish(claim, result="late result")
    assert repository.get(scope, run.run_id).status == RunStatus.CANCELLED


def test_repeated_crashes_stop_at_retry_limit(repository, scope, clock):
    run = repository.create(scope, "crashing")
    for _ in range(3):
        assert repository.claim_next() is not None
        clock.advance(61)
    assert repository.claim_next() is None
    result = repository.get(scope, run.run_id)
    assert result.status == RunStatus.FAILED
    assert result.error_code == "retry_limit_exceeded"


def test_unknown_tool_is_rejected_without_execution(repository, scope):
    class UnsafeModel:
        def plan(self, prompt):
            return ToolCall("shell", "do not run this")

    run = repository.create(scope, "test")
    Worker(repository, Harness(UnsafeModel())).run_once()
    assert repository.get(scope, run.run_id).error_code == "execution_failed"
    assert [e.event_type for e in repository.list_events(scope, run.run_id)] == [
        "queued",
        "running",
        "failed",
    ]


def test_state_and_events_roll_back_together(repository, scope, monkeypatch):
    import agentforge_storage.repository as module

    def fail_event(*args):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(module, "_event", fail_event)
    try:
        repository.create(scope, "must roll back")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected storage failure")
    with repository.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(module.runs)).scalar() == 0
        assert connection.execute(sa.select(sa.func.count()).select_from(events)).scalar() == 0


def test_renewal_extends_only_a_live_owned_lease(repository, scope, clock):
    run = repository.create(scope, "renew")
    claim = repository.claim_next(lease_seconds=3)
    clock.advance(2)
    assert repository.renew_lease(claim, lease_seconds=3)
    clock.advance(2)
    assert repository.claim_next() is None
    repository.cancel(scope, run.run_id)
    assert not repository.renew_lease(claim)


def test_expired_attempt_cannot_be_revived_by_heartbeat(repository, scope, clock):
    repository.create(scope, "expired renewal")
    old = repository.claim_next(lease_seconds=2)
    clock.advance(3)
    assert not repository.renew_lease(old)
    new = repository.claim_next()
    assert not repository.renew_lease(old)
    assert repository.renew_lease(new)


def test_worker_renews_during_model_processing(repository, scope, monkeypatch):
    renewed = Event()
    original = repository.renew_lease

    def renew(*args):
        result = original(*args)
        renewed.set()
        return result

    class WaitingModel:
        def plan(self, prompt):
            if not renewed.wait(2):
                raise AssertionError("Worker did not renew its lease during model work")
            return ToolCall("example_report", prompt)

    monkeypatch.setattr(repository, "renew_lease", renew)
    run = repository.create(scope, "slow model")
    Worker(repository, Harness(WaitingModel()), heartbeat_interval=0.01).run_once()
    assert renewed.is_set()
    assert repository.get(scope, run.run_id).status == RunStatus.SUCCEEDED


def test_failed_heartbeat_blocks_later_tool_execution(repository, scope, monkeypatch):
    import agentforge_worker.worker as module

    heartbeats = []

    class ObservedHeartbeat(LeaseHeartbeat):
        def __enter__(self):
            heartbeats.append(self)
            return super().__enter__()

    def fail_renewal(*args):
        raise OperationalError("private", {}, RuntimeError("lost connection"))

    class WaitingModel:
        def plan(self, prompt):
            if not heartbeats[0].lost.wait(2):
                raise AssertionError("Expected lease loss")
            return ToolCall("example_report", prompt)

    monkeypatch.setattr(module, "LeaseHeartbeat", ObservedHeartbeat)
    monkeypatch.setattr(repository, "renew_lease", fail_renewal)
    run = repository.create(scope, "lost lease")
    Worker(repository, Harness(WaitingModel()), heartbeat_interval=0.01).run_once()
    assert [e.event_type for e in repository.list_events(scope, run.run_id)] == [
        "queued",
        "running",
    ]
    assert repository.get(scope, run.run_id).result is None


def test_watch_processes_tasks_then_drains_on_shutdown(repository, scope):
    stop = Event()
    completed = []

    class StoppingHarness(Harness):
        def execute(self, prompt, before_tool):
            result = super().execute(prompt, before_tool)
            completed.append(prompt)
            if len(completed) == 2:
                stop.set()
            return result

    submitted = [repository.create(scope, f"task-{index}") for index in range(3)]
    Worker(repository, StoppingHarness()).run_forever(stop)
    statuses = [repository.get(scope, run.run_id).status for run in submitted]
    assert statuses.count(RunStatus.SUCCEEDED) == 2
    assert statuses.count(RunStatus.QUEUED) == 1


def test_idle_watch_wakes_immediately_on_shutdown(repository, monkeypatch):
    stop = Event()
    queried = Event()

    def idle(**kwargs):
        queried.set()
        return None

    monkeypatch.setattr(repository, "claim_next", idle)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Worker(repository).run_forever, stop, poll_interval=3600)
        try:
            assert queried.wait(2)
        finally:
            stop.set()
        future.result(timeout=2)


def test_watch_backs_off_after_storage_failure(repository, monkeypatch):
    stop = Event()
    waits = []

    def unavailable(**kwargs):
        raise OperationalError("private", {}, RuntimeError("unavailable"))

    def wait(interval):
        waits.append(interval)
        stop.set()

    monkeypatch.setattr(repository, "claim_next", unavailable)
    monkeypatch.setattr(stop, "wait", wait)
    Worker(repository).run_forever(stop, poll_interval=5)
    assert waits == [5]


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), 60])
def test_invalid_heartbeat_configuration_fails_early(repository, interval):
    with pytest.raises(ValueError):
        Worker(repository, heartbeat_interval=interval)

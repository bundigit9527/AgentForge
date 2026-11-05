import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from agentforge_core.desktop import slot_manifest
from agentforge_supervisor import desktop_pool, workspace_checkpoint
from agentforge_supervisor.workspace_checkpoint import CheckpointError, WorkspaceCheckpoint


def test_publish_is_atomic_and_bounded(tmp_path, monkeypatch):
    checkpoint = WorkspaceCheckpoint(tmp_path, Path("a.json"), "g", "image")
    checkpoint._publish([sys.executable, "-c", "print('previous')"])
    monkeypatch.setattr(workspace_checkpoint, "MAX_ARCHIVE", 12)
    for script, error in [
        ("print('partial'); raise SystemExit(1)", RuntimeError),
        ("print('x'*64)", ValueError),
    ]:
        with pytest.raises(error):
            checkpoint._publish([sys.executable, "-c", script])
        assert checkpoint.archive.read_bytes() == b"previous\n"
        assert not checkpoint.partial.exists()


def test_checkpoint_failure_unpauses_and_preserves_live_slot(tmp_path, monkeypatch):
    store = tmp_path / "checkpoints"
    pool = desktop_pool.DesktopPool(tmp_path, "sha256:" + "a" * 64, 2, store)
    owner = ["tenant", "space", "user", "00000000-0000-0000-0000-000000000001"]
    path = slot_manifest(tmp_path, owner)
    gen = "a" * 32
    path.write_text('{"generation":"' + gen + '"}')
    journal = path.with_suffix(".pool-generation")
    journal.write_text(gen)
    live = Mock()
    pool.live[path] = live
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:2] == ("volume", "ls"):
            return "agentforge-work-" + gen
        if args[:2] == ("ps", "-q"):
            return "running"
        return ""

    monkeypatch.setattr(desktop_pool, "docker", docker)
    monkeypatch.setattr(workspace_checkpoint, "docker", docker)

    def fail_publish(self, command):
        raise OSError("disk full")

    monkeypatch.setattr(WorkspaceCheckpoint, "_publish", fail_publish)
    with pytest.raises(CheckpointError):
        pool.dispatch(["stop", owner])
    assert ("unpause", "agentforge-desktop-" + gen) in calls
    assert not any(call[:2] == ("rm", "-f") for call in calls)
    live.close.assert_not_called()
    assert path.exists() and journal.exists()


def test_unready_generation_does_not_overwrite_checkpoint(tmp_path, monkeypatch):
    store = tmp_path / "checkpoints"
    pool = desktop_pool.DesktopPool(tmp_path, "sha256:" + "a" * 64, 2, store)
    path = tmp_path / "scope.json"
    path.with_suffix(".pool-generation").write_text("b" * 32)
    (store / "scope.tar").write_bytes(b"older")
    monkeypatch.setattr(desktop_pool, "docker", lambda *args: "")
    monkeypatch.setattr(workspace_checkpoint, "docker", lambda *args: "")
    monkeypatch.setattr(
        WorkspaceCheckpoint, "save", lambda *a, **kw: pytest.fail("saved incomplete boot")
    )
    pool.recover()
    assert (store / "scope.tar").read_bytes() == b"older"
    assert not path.with_suffix(".pool-generation").exists()

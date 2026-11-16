import pytest
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.workspace import OpenPath
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_runtime.workspace_tools import with_workspace_tools
from agentforge_supervisor import workspace_preview

from tests.test_workspace_file_tools import (
    answer,
    scripted_worker,
    submit,
    write_args,
)
from tests.test_workspace_file_tools import browser_env as _browser_env
from tests.test_workspace_file_tools import people as _people
from tests.test_workspace_file_tools import workspace_env as _workspace_env

browser_env = _browser_env
people = _people
workspace_env = _workspace_env


def test_office_without_app_is_explicit_failure(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = workspace_preview.open_document(b"bytes", ".pptx", "report.pptx")
    assert result["previewed"] is False
    assert result["error"] == "workspace_preview_application_missing"


@pytest.mark.parametrize("path", ["https://example.com", "/etc/passwd", "../other.txt"])
def test_open_rejects_url_absolute_and_parent_paths(path):
    with pytest.raises(ValueError):
        OpenPath(path=path, computer_mode="team")


def test_default_snapshot_has_no_open_tool():
    assert not ExecutionSnapshot().workspace_open
    assert "open_path@1" not in ExecutionSnapshot().tool_references


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_open_uses_original_approval_and_scoped_file_program(
    workspace_env, repository, monkeypatch, decision
):
    monkeypatch.setenv("AGENTFORGE_WORKSPACE_OPEN_ENABLED", "true")
    rendered = []

    def render(raw, suffix, path):
        rendered.append((raw, suffix, path))
        return dict(path=path, pages=1, truncated=False, previewed=True, visual_reviewed=False)

    monkeypatch.setattr(
        "agentforge_supervisor.workspace_files.open_document", render, raising=False
    )
    rid = submit(workspace_env)
    worker, _ = scripted_worker(
        repository,
        [
            ("write_file", write_args("report")),
            ("open_path", dict(path="note.txt", computer_mode="team")),
        ],
    )
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert not rendered
    answer(workspace_env, repository, rid, decision)
    worker.run_once()
    assert len(rendered) == (1 if decision == "allow" else 0)
    if rendered:
        assert rendered[0] == (b"report", ".txt", "note.txt")
    assert repository.get(workspace_env[3], rid).status == "succeeded"


def test_registry_open_is_write_not_unapproved_read():
    registry = with_workspace_tools(
        BUILTIN_TOOLS, lambda *args: {}, "team", editing=True, aligned=True, opening=True
    )
    from agentforge_runtime.errors import RuntimeFailure

    with pytest.raises(RuntimeFailure):
        ToolExecutor(
            ("open_path@1",),
            registry=registry,
            allowed=("open_path@1",),
            read_allowed=("open_path@1",),
        )


@pytest.mark.parametrize("stop", ["unapproved", "disabled", "cancelled", "expired"])
def test_open_cannot_bypass_live_authorization(workspace_env, repository, monkeypatch, clock, stop):
    from tests.test_workspace_bridge import claim_tool, execute

    monkeypatch.setenv("AGENTFORGE_WORKSPACE_OPEN_ENABLED", "true")
    claim, args = claim_tool(
        workspace_env, repository, "open_path", dict(path="note.txt", computer_mode="team")
    )
    if stop == "disabled":
        monkeypatch.setenv("AGENTFORGE_WORKSPACE_OPEN_ENABLED", "false")
    elif stop == "cancelled":
        repository.cancel(workspace_env[3], claim.run.run_id)
    elif stop == "expired":
        clock.advance(1000)
    assert execute(workspace_env[0], claim, "open_path", args).status_code == 403
    assert workspace_env[6] == []


def test_cancel_after_preview_keeps_unknown_effect(workspace_env, repository, monkeypatch):
    import sqlalchemy as sa
    from agentforge_storage.schema import external_effects

    monkeypatch.setenv("AGENTFORGE_WORKSPACE_OPEN_ENABLED", "true")
    rid = submit(workspace_env)
    rendered = []

    def render(raw, suffix, path):
        rendered.append(path)
        repository.cancel(workspace_env[3], rid)
        return dict(path=path, pages=1, truncated=False, previewed=True, visual_reviewed=False)

    monkeypatch.setattr(
        "agentforge_supervisor.workspace_files.open_document", render, raising=False
    )
    worker, _ = scripted_worker(
        repository,
        [
            ("write_file", write_args("report")),
            ("open_path", dict(path="note.txt", computer_mode="team")),
        ],
    )
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(workspace_env[3], rid).status == "cancelled"
    assert rendered == ["note.txt"]
    with repository.engine.connect() as conn:
        statuses = (
            conn.execute(
                sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
            )
            .scalars()
            .all()
        )
    assert sorted(statuses) == ["dispatched", "succeeded"]
    assert not worker.run_once()
    assert rendered == ["note.txt"]

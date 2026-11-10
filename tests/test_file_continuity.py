import base64
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_connectors.python_execution import PythonService
from agentforge_core.models import RunNotFound, RunStatus
from agentforge_core.python_execution import (
    EncodedFile,
    InputFile,
    PythonArgumentsV2,
    PythonJob,
    PythonResponse,
)
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.python_tools import with_python_tool
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_storage.schema import artifact_revisions
from agentforge_worker.tools import RunToolJournal
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_orpc import create_bot, rpc
from tests.test_python_execution import IMAGE
from tests.test_python_execution import python_grant as _python_grant

python_grant = _python_grant


def uploaded(client, headers, bot, data=b"original", name="report.txt"):
    response = rpc(
        client,
        headers,
        "artifacts.create",
        dict(
            botId=bot,
            name=name,
            mimeType="text/plain",
            contentBase64=base64.b64encode(data).decode(),
        ),
    )
    assert response.is_success, response.text
    return response.json()["json"]["id"]


def send(client, headers, bot, **kwargs):
    response = rpc(client, headers, "threads.send", dict(botId=bot, text="edit file", **kwargs))
    assert response.is_success, response.text
    return response.json()["json"]["runId"]


def setup_claim(settings, repository, headers, scope):
    client = TestClient(create_app(settings, repository, model_settings=model_settings()))
    bot = create_bot(client, headers)["id"]
    original = uploaded(client, headers, bot)
    rid = send(client, headers, bot, artifactIds=[original])
    claim = repository.claim_next()
    assert claim.run.run_id == rid
    return client, bot, original, claim


def executor_for(repository, claim):
    service = PythonService(claim.run.scope, IMAGE, lambda: repository.owns_claim(claim))
    registry = with_python_tool(
        BUILTIN_TOOLS, service, load_inputs=lambda inputs: repository.python_inputs(claim, inputs)
    )
    executor = ToolExecutor(
        ("python_execute@2",),
        registry=registry,
        allowed=("python_execute@2",),
        journal=RunToolJournal(repository, claim),
        checkpoint=lambda: repository.owns_claim(claim),
    )
    return executor, service


def response(name="report.txt", data=b"modified"):
    return PythonResponse(
        exit_code=0,
        stdout="checked",
        stderr="",
        files=[EncodedFile(name=name, data=base64.b64encode(data).decode())],
    )


def test_two_turn_continuity_native_versions_and_immutable_download(
    settings, repository, headers, scope, python_grant
):
    gateway = model_settings()
    files = ArtifactRepository(repository)
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)["id"]
        original = uploaded(client, headers, bot)
        previous = original
        for iteration in (1, 2):
            payload = dict(clientNonce=f"turn-{iteration}")
            if iteration == 1:
                payload["artifactIds"] = [original]
            rid = send(client, headers, bot, **payload)
            run = repository.get(scope, rid)
            assert run.execution_config.python_continuity
            assert [f.artifact_id for f in run.execution_config.python_files] == [previous]
            calls = []

            def model(request, calls=calls, previous=previous):
                body = json.loads(request.content)
                if not calls:
                    assert previous in str(body["messages"])
                    calls.append(1)
                    return httpx.Response(
                        200,
                        json=completion(
                            None,
                            [
                                tool_call(
                                    "edit",
                                    "python_execute",
                                    json.dumps(
                                        dict(
                                            code="edit exact input",
                                            exports=["report.txt"],
                                            inputs=[dict(artifact_id=previous, name="report.txt")],
                                        )
                                    ),
                                )
                            ],
                        ),
                    )
                receipt = json.loads(body["messages"][-1]["content"])
                assert receipt["files"][0]["parent_id"] == previous
                return httpx.Response(200, json=completion("已修改，见新版本附件"))

            def compute(self, arguments, iteration=iteration):
                self.authorized()
                expected = b"original" if iteration == 1 else b"modified1"
                assert base64.b64decode(arguments["inputs"][0]["data"]) == expected
                return response(data=f"modified{iteration}".encode())

            worker = Worker(
                repository,
                harness_factory=lambda _: configured_harness(
                    gateway, transport=httpx.MockTransport(model)
                ),
            )
            with patch.object(PythonService, "execute", compute):
                assert worker.run_once()
            assert repository.get(scope, rid).status == RunStatus.SUCCEEDED
            versions = rpc(
                client, headers, "artifacts.listVersions", dict(familyId=original)
            ).json()["json"]
            assert [v["version"] for v in versions] == list(range(iteration + 1, 0, -1))
            previous = versions[0]["id"]
            assert send(client, headers, bot, **payload) == rid
            assert repository.get(scope, rid).execution_config == run.execution_config
        assert base64.b64decode(files.get(scope, original)["contentBase64"]) == b"original"
        assert base64.b64decode(files.get(scope, previous)["contentBase64"]) == b"modified2"
        page = rpc(client, headers, "artifacts.listSpace", dict(limit=1)).json()["json"]
        assert len(page["items"]) == 1 and page["items"][0]["versionCount"] == 3
        assert page["items"][0]["id"] == previous and page["nextCursor"] is None
        assert len(rpc(client, headers, "artifacts.list", dict(botId=bot)).json()["json"]) == 3
        assert (
            len(
                rpc(client, headers, "artifacts.listVersions", dict(familyId=previous)).json()[
                    "json"
                ]
            )
            == 3
        )
        rpc(client, headers, "threads.clear", dict(botId=bot))
        rid = send(client, headers, bot)
        assert repository.get(scope, rid).execution_config.python_files == ()


def test_same_run_reuse_replay_and_explicit_old_base_are_immutable(
    settings, repository, headers, scope, python_grant
):
    client, bot, original, claim = setup_claim(settings, repository, headers, scope)
    executor, service = executor_for(repository, claim)

    def prepared(call_id, parent):
        return executor.prepare(
            call_id,
            "python_execute",
            json.dumps(
                dict(
                    code="edit",
                    exports=["report.txt"],
                    inputs=[dict(artifact_id=parent, name="report.txt")],
                )
            ),
        )

    first = prepared("first", original)
    with patch.object(service, "execute", return_value=response()) as compute:
        result = executor.execute(first)
        assert executor.execute(first) == result and compute.call_count == 1
        new = json.loads(result)["files"][0]["artifact_id"]
        executor.execute(prepared("second", new))
        # Editing an explicit older base creates a distinct immutable revision, not an overwrite.
        executor.execute(prepared("branch", original))
    versions = ArtifactRepository(repository).versions(scope, original)
    assert [v["version"] for v in versions] == [4, 3, 2, 1]
    with repository.engine.connect() as conn:
        parents = conn.execute(sa.select(artifact_revisions)).mappings().all()
    assert next(r for r in parents if r["version"] == 4)["parent_id"] == original
    assert (
        base64.b64decode(ArtifactRepository(repository).get(scope, original)["contentBase64"])
        == b"original"
    )
    client.close()


@pytest.mark.parametrize(
    "kind", ["unselected", "other_bot", "other_user", "other_space", "cancel", "lease"]
)
def test_input_authorization_and_lease(
    settings, repository, headers, scope, python_grant, clock, kind
):
    client, bot, original, claim = setup_claim(settings, repository, headers, scope)
    ref = original
    if kind == "unselected":
        ref = uploaded(client, headers, bot)
    elif kind == "other_bot":
        ref = uploaded(client, headers, create_bot(client, headers)["id"])
    elif kind == "other_user":
        claim = replace(claim, run=replace(claim.run, scope=replace(scope, user_id="other")))
    elif kind == "other_space":
        claim = replace(claim, run=replace(claim.run, scope=replace(scope, space_id="other")))
    elif kind == "cancel":
        repository.cancel(scope, claim.run.run_id)
    else:
        clock.advance(100)
    with pytest.raises(RunNotFound):
        repository.python_inputs(claim, [InputFile(artifact_id=ref, name="x.txt")])
    client.close()


@pytest.mark.parametrize("name", ["../x.txt", "/x.txt", "_agentforge_job.py", "a\\b", "\x00bad"])
def test_input_alias_validation(name):
    with pytest.raises(ValueError):
        PythonArgumentsV2(code="x", inputs=[dict(artifact_id=str(uuid4()), name=name)])


def test_supervisor_input_limits_and_duplicate_alias():
    base = dict(image=IMAGE, code="x")
    with pytest.raises(ValueError):
        PythonJob(**base, inputs=[dict(name="a", data="!!!!")])
    with pytest.raises(ValueError):
        PythonJob(**base, inputs=[dict(name="a", data="YQ==")] * 2)
    with pytest.raises(ValueError):
        PythonJob(
            **base,
            inputs=[dict(name="a", data=base64.b64encode(b"a" * (10 * 1024 * 1024 + 1)).decode())],
        )


def test_concurrent_revisions_keep_both_outputs(settings, repository, headers, scope, python_grant):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agentforge_storage.conversations import ConversationRepository

    client, bot, original, first_claim = setup_claim(settings, repository, headers, scope)
    thread = ConversationRepository(repository.engine, repository.clock).create_thread(
        scope, bot, "Parallel"
    )
    repository.create(
        scope,
        "parallel edit",
        thread_id=thread["thread_id"],
        _artifact_ids=(original,),
        _resolve_config=lambda conn, config: first_claim.run.execution_config,
    )
    second_claim = repository.claim_next()
    barrier = Barrier(2)

    def edit(claim):
        executor, service = executor_for(repository, claim)
        call = executor.prepare(
            "edit",
            "python_execute",
            json.dumps(
                dict(
                    code="edit",
                    exports=["report.txt"],
                    inputs=[dict(artifact_id=original, name="report.txt")],
                )
            ),
        )

        def compute(args):
            barrier.wait(timeout=10)
            return response(data=claim.run.run_id.encode())

        with patch.object(service, "execute", compute):
            return json.loads(executor.execute(call))["files"][0]["artifact_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(edit, [first_claim, second_claim]))
    files = ArtifactRepository(repository)
    versions = files.versions(scope, original)
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert {v["id"] for v in versions[:-1]} == set(ids)
    assert {base64.b64decode(files.get(scope, ref)["contentBase64"]).decode() for ref in ids} == {
        first_claim.run.run_id,
        second_claim.run.run_id,
    }
    with pytest.raises(RunNotFound):
        repository.python_inputs(first_claim, [InputFile(artifact_id=ids[1], name="report.txt")])
    client.close()


def test_publish_rolls_back_file_and_version_when_event_fails(
    settings, repository, headers, scope, python_grant
):
    client, bot, original, claim = setup_claim(settings, repository, headers, scope)
    executor, service = executor_for(repository, claim)
    call = executor.prepare(
        "edit",
        "python_execute",
        json.dumps(
            dict(
                code="edit",
                exports=["report.txt"],
                inputs=[dict(artifact_id=original, name="report.txt")],
            )
        ),
    )
    with (
        patch.object(service, "execute", return_value=response()),
        patch(
            "agentforge_storage.artifacts.append", side_effect=RuntimeError("event write failed")
        ),
        pytest.raises(RuntimeError),
    ):
        executor.execute(call)
    assert [v["id"] for v in ArtifactRepository(repository).versions(scope, original)] == [original]
    assert repository.list_tool_calls(scope, claim.run.run_id)[0]["status"] == "running"
    with repository.engine.connect() as conn:
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(artifact_revisions)).scalar_one()
            == 0
        )
    client.close()


def test_legacy_recipe_and_tool_stay_resolvable(repository, scope):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_skills.builtin import LEGACY_BUILTIN_SKILLS
    from agentforge_skills.service import SkillService
    from agentforge_storage.skills import SkillRepository

    # No Hub access is performed for builtin resolution.
    skills = SkillService(SkillRepository(repository.engine, repository.clock), None)
    legacy = skills.resolve(scope, ["builtin:presentation@1"])[0]
    assert legacy == LEGACY_BUILTIN_SKILLS[0]
    config = ExecutionSnapshot(
        model=model_settings().snapshot(), python_image=IMAGE, skills=(legacy,)
    )
    assert config.python_tool == "python_execute@1" and config.python_files == ()

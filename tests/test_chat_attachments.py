import base64
import json

import httpx
from agentforge_api.app import create_app
from agentforge_runtime.chat_harness import configured_harness
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc

enterprise = _enterprise
people = _people


def test_only_attachment_to_tool_result_file(settings, repository, headers):
    gateway = model_settings()
    text = "x" * 1999 + "营收,18\n"
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)["id"]

        def call(method, **data):
            return rpc(client, headers, method, {"botId": bot, **data})

        upload = dict(
            name="数据.csv",
            mimeType="text/csv",
            contentBase64=base64.b64encode(text.encode()).decode(),
            clientNonce="upload",
        )
        file = call("artifacts.create", **upload).json()["json"]
        assert file["size"] == len(text.encode()) and file["runId"] is None
        assert call("artifacts.create", **upload).json()["json"] == file
        assert call("artifacts.create", **{**upload, "name": "changed.csv"}).status_code == 409
        data = dict(artifactIds=[file["id"]], clientNonce="message")
        receipt = call("threads.send", **data).json()["json"]
        assert call("threads.send", **data).json()["json"] == receipt
        assert call("threads.send", text="different", clientNonce="message").status_code == 409
        responses = []

        def model(request):
            body = json.loads(request.content)
            step = len(responses)
            responses.append(step)
            if step == 0:
                assert file["id"] in body["messages"][-1]["content"]
            if step in (0, 1):
                offset = (
                    0 if not step else json.loads(body["messages"][-1]["content"])["next_offset"]
                )
                if step:
                    assert offset == 1999
                message = tool_call(
                    f"read-{step}",
                    "read_attachment",
                    json.dumps({"artifact_id": file["id"], "offset": offset}),
                )
            elif step == 2:
                actual = json.loads(body["messages"][-1]["content"])["content"]
                assert actual == "营收,18\n"
                message = tool_call(
                    "write",
                    "write_attachment",
                    json.dumps({"name": "result.md", "mime_type": "text/markdown", "text": actual}),
                )
            else:
                return httpx.Response(200, json=completion("已生成 result.md"))
            return httpx.Response(200, json=completion(None, [message]))

        worker = Worker(
            repository,
            harness_factory=lambda config: configured_harness(
                gateway, transport=httpx.MockTransport(model)
            ),
        )
        assert worker.run_once()
        state = call("threads.get").json()["json"]
        assert state["run"]["status"] == "completed" and len(responses) == 4
        assert state["messages"][0]["blocks"] == [
            {
                "kind": "file",
                "artifactId": file["id"],
                "name": "数据.csv",
                "mimeType": "text/csv",
                "size": len(text.encode()),
            }
        ]
        result = next(
            m["blocks"][0]
            for m in state["messages"]
            if m["role"] == "bot" and m["blocks"][0]["kind"] == "file"
        )
        got = call("artifacts.get", artifactId=result["artifactId"]).json()["json"]
        assert (
            got["runId"] == receipt["runId"]
            and base64.b64decode(got["contentBase64"]).decode() == "营收,18\n"
        )
        assert len(call("artifacts.list").json()["json"]) == 2
        assert call("threads.send", **data).json()["json"] == receipt


def test_foreign_and_invalid_uploads(enterprise):
    client, headers, *_ = enterprise
    alice, bob = headers["alice"], headers["bob"]
    a, b = create_bot(client, alice)["id"], create_bot(client, bob)["id"]
    meta = dict(botId=a, name="a.txt", mimeType="text/plain", contentBase64="YQ==")
    file = rpc(client, alice, "artifacts.create", meta).json()["json"]["id"]
    assert rpc(client, bob, "artifacts.getById", {"artifactId": file}).status_code == 404
    assert rpc(client, bob, "threads.send", {"botId": b, "artifactIds": [file]}).status_code == 404
    assert rpc(client, bob, "threads.send", {"botId": b, "text": "ok"}).is_success
    other = create_bot(client, alice)["id"]
    assert (
        rpc(client, alice, "threads.send", {"botId": other, "artifactIds": [file]}).status_code
        == 404
    )
    nonce = {**meta, "clientNonce": "unique"}
    assert rpc(client, alice, "artifacts.create", nonce).is_success
    assert rpc(client, alice, "artifacts.create", {**nonce, "botId": other}).status_code == 404
    for changes in (
        {"contentBase64": "!"},
        {"name": "../secret"},
        {"mimeType": "application/x-sh"},
    ):
        assert rpc(client, alice, "artifacts.create", {**meta, **changes}).status_code == 422


def test_attachment_size_and_nonce_identity(client, headers):
    bot = create_bot(client, headers)["id"]

    def upload(blob):
        return rpc(
            client,
            headers,
            "artifacts.create",
            dict(
                botId=bot,
                name="raw.zip",
                mimeType="application/zip",
                contentBase64=base64.b64encode(blob).decode(),
            ),
        )

    one = upload(b"\x00" * 140_000)
    assert one.is_success
    ref = one.json()["json"]["id"]
    two = upload(b"\xffraw").json()["json"]["id"]
    assert upload(b"x" * (10 * 1024 * 1024 + 1)).status_code in (413, 422)
    kwargs = {"botId": bot, "text": "read", "clientNonce": "same"}
    assert rpc(client, headers, "threads.send", {**kwargs, "artifactIds": [ref]}).is_success
    assert rpc(client, headers, "threads.send", {**kwargs, "artifactIds": [two]}).status_code == 409
    assert (
        rpc(client, headers, "threads.send", {**kwargs, "artifactIds": [ref] * 5}).status_code
        == 422
    )
    assert (
        rpc(client, headers, "artifacts.getById", {"artifactId": two}).json()["json"][
            "contentBase64"
        ]
        == base64.b64encode(b"\xffraw").decode()
    )


def test_reader_rejects_an_unattached_file_before_loading_bytes():
    import pytest
    from agentforge_runtime.attachment_tools import READ_ATTACHMENT, with_attachment_tools
    from agentforge_runtime.errors import RuntimeFailure
    from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor

    registry = with_attachment_tools(
        BUILTIN_TOOLS,
        ({"artifactId": "selected", "mimeType": "application/pdf"},),
        lambda ref, pos: (b"\x00\xff", 2),
    )
    executor = ToolExecutor(
        (READ_ATTACHMENT,),
        registry=registry,
        allowed=(READ_ATTACHMENT,),
        read_allowed=(READ_ATTACHMENT,),
    )
    with pytest.raises(RuntimeFailure):
        executor.prepare("1", "read_attachment", '{"artifact_id":"unattached"}')
    read = executor.prepare("2", "read_attachment", '{"artifact_id":"selected"}')
    output = json.loads(executor.execute(read))
    assert output == {"encoding": "base64", "content": "AP8=", "next_offset": None}

import base64
import json
from dataclasses import replace

from agentforge_api.app import create_app
from agentforge_api.desktop import Desktop
from agentforge_core.execution import ExecutionSnapshot
from agentforge_runtime.config import ModelSettings
from agentforge_storage.conversations import ConversationRepository
from agentforge_supervisor.workspace_files import operate
from fastapi.testclient import TestClient

from tests.test_browser_auth import ORIGIN, login
from tests.test_identity import people as _people
from tests.test_orpc import rpc

people = _people


def test_original_files_rpc_contract(settings, repository, people, tmp_path, monkeypatch):
    who = people[1]["alice"]
    bot = ConversationRepository(repository.engine, repository.clock).create_agent(
        who, "Files", ExecutionSnapshot(model=ModelSettings().snapshot())
    )["agent_id"]
    owner = dict(tenant=who.tenant_id, space=who.space_id, user=who.user_id, bot=bot)
    slots = tmp_path / "slots.json"
    slots.write_text(json.dumps([{**owner, "manifest": str(tmp_path / "alice.json")}]))
    monkeypatch.setenv("AGENTFORGE_DESKTOP_SLOTS", str(slots))
    monkeypatch.delenv("AGENTFORGE_DESKTOP_MANIFEST", raising=False)
    monkeypatch.setenv("SCREEN_PROXY_SECRET", "x" * 32)
    folder = tmp_path / "files"
    folder.mkdir()

    def exchange(self, action, *params):
        if action == "status":
            return {"entry": {**owner, "generation": "gen"}}
        generation, data = params
        assert generation == "gen"
        return {"result": operate(action, data, folder)}

    monkeypatch.setattr(Desktop, "exchange", exchange)
    app = create_app(
        replace(settings, auth_mode="database", web_origin=ORIGIN),
        repository,
        model_settings=ModelSettings(),
    )
    headers = {"Origin": ORIGIN}
    with TestClient(app) as client:
        assert login(client, people[2]["alice"]["token"]).is_success

        def call(action, **kw):
            return rpc(client, headers, "computer." + action, {"botId": bot, **kw})

        content = "财经\n" * 32000
        upload = {"path": "财务.txt", "contentBase64": base64.b64encode(content.encode()).decode()}
        assert call("files", path="").json()["json"] == []
        assert call("uploadFile", **upload).status_code == 403
        assert call("takeover").is_success
        assert call("uploadFile", **upload).json()["json"] == {"ok": True}
        assert call("files").json()["json"][0]["path"] == upload["path"]
        assert call("readFile", path=upload["path"]).json()["json"]["content"] == content
        assert call("downloadFile", path=upload["path"]).json()["json"] == upload
        assert call("release").is_success
        assert call("uploadFile", **upload).status_code == 403
        assert login(client, people[2]["bob"]["token"]).is_success
        assert call("downloadFile", path=upload["path"]).status_code == 404


def test_team_private_switch_and_control(settings, repository, people, tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from uuid import uuid4

    monkeypatch.delenv("AGENTFORGE_DESKTOP_SLOTS", raising=False)
    monkeypatch.delenv("AGENTFORGE_DESKTOP_MANIFEST", raising=False)
    monkeypatch.setenv("AGENTFORGE_DESKTOP_POOL", str(tmp_path / "pool.sock"))
    monkeypatch.setenv("SCREEN_PROXY_SECRET", "s" * 32)
    running = {}

    def exchange(self, action, *params):
        key = tuple(self.owner)
        if action == "boot" and key not in running:
            entry = dict(zip(("tenant", "space", "user", "bot"), key, strict=True))
            running[key] = dict(
                entry, generation=uuid4().hex, port=6012, terminal_port=6100, view="v", control="c"
            )
        if action == "stop":
            running.pop(key, None)
        return {"entry": running.get(key)}

    monkeypatch.setattr(Desktop, "exchange", exchange)
    app = create_app(
        replace(settings, auth_mode="database", web_origin=ORIGIN),
        repository,
        model_settings=ModelSettings(),
    )
    headers = {"Origin": ORIGIN}
    with TestClient(app) as alice, TestClient(app) as bob:
        assert login(alice, people[2]["alice"]["token"]).is_success
        assert login(bob, people[2]["bob"]["token"]).is_success

        def call(client, method, **data):
            return rpc(client, headers, method, data)

        a = call(alice, "bots.create", name="A").json()["json"]["id"]
        b = call(bob, "bots.create", name="B", computerMode="team").json()["json"]["id"]
        private = call(alice, "bots.create", name="private", computerMode="dedicated").json()[
            "json"
        ]
        assert private["computerMode"] == "dedicated"
        for client, bot in [(alice, a), (bob, b)]:
            assert call(client, "computer.boot", botId=bot).json()["json"]["mode"] == "team"
        assert len(running) == 1
        assert call(bob, "computer.boot", botId=a).status_code == 404
        assert call(alice, "computer.takeover", botId=a).is_success
        assert call(bob, "computer.takeover", botId=b).status_code == 409
        assert call(bob, "computer.stop", botId=b).status_code == 409
        assert call(bob, "computer.terminalUrl", botId=b).status_code == 403
        assert call(alice, "computer.commands", botId=a).json()["json"] == []
        assert call(alice, "computer.status", botId=a).json()["json"]["terminalAvailable"]
        term = call(alice, "computer.terminalUrl", botId=a).json()["json"]["url"]
        key = term.split("/")[-2]
        generation = next(iter(running.values()))["generation"]

        def access(k, gen):
            return alice.post("/api/internal/terminal-access", json={"key": k, "generation": gen})

        assert access(key, generation).json() == {"workspaceBot": a}
        assert access(key, "0" * 32).status_code == 403
        screen = call(alice, "computer.screenUrl", botId=a).json()["json"]["url"]
        assert access(screen.split("/")[-2], generation).status_code == 403
        path = term.rsplit("/", 1)[0] + "/websockify"
        route = alice.post(
            "/api/internal/screen-target",
            headers={"authorization": "Bearer " + "s" * 32},
            json={"path": path},
        ).json()
        assert route["port"] == 6100 and route["path"] == f"/terminal/{key}"
        assert call(alice, "bots.setComputer", botId=a, mode="dedicated").status_code == 409
        assert call(alice, "computer.release", botId=a).is_success
        assert access(key, generation).status_code == 403
        url = call(alice, "computer.screenUrl", botId=a).json()["json"]["url"]

        def target():
            return alice.post(
                "/api/internal/screen-target",
                headers={"authorization": "Bearer " + "s" * 32},
                json={"path": urlsplit(url).path},
            )

        assert target().is_success
        assert (
            call(alice, "bots.setComputer", botId=a, mode="dedicated").json()["json"][
                "computerMode"
            ]
            == "dedicated"
        )
        assert target().status_code == 403
        assert call(alice, "computer.boot", botId=a).json()["json"]["mode"] == "dedicated"
        assert len(running) == 2
        assert call(alice, "bots.setComputer", botId=a, mode="team").status_code == 409
        assert call(alice, "computer.stop", botId=a).is_success
        assert call(alice, "bots.setComputer", botId=a, mode="team").is_success
        assert target().status_code == 403
        assert call(bob, "computer.status", botId=b).json()["json"]["state"] == "running"

"""Visible browser scope, lease, real tool loop and outbound boundary tests."""

import base64
import json
import socket
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_api.desktop import Desktop
from agentforge_connectors.browser import BrowserPolicy
from agentforge_core.browser import BrowserInput, permitted_url
from agentforge_core.execution import ExecutionSnapshot
from agentforge_runtime.chat_harness import configured_harness
from agentforge_storage.rakazo import RakazoRepository
from agentforge_supervisor import browser_fetch
from agentforge_supervisor.browser_pool import BrowserPool
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_browser_auth import ORIGIN, login
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc

people = _people
TOKEN = "b" * 48
PAGE = dict(
    snapshot="a" * 32,
    url="https://example.com/",
    title="Public page",
    text="Source",
    links=[dict(id="1", text="Next", url="https://example.org/next")],
)


@pytest.fixture
def browser_env(settings, repository, people, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFORGE_DESKTOP_SLOTS", raising=False)
    monkeypatch.delenv("AGENTFORGE_DESKTOP_MANIFEST", raising=False)
    monkeypatch.setenv("AGENTFORGE_DESKTOP_POOL", str(tmp_path / "pool.sock"))
    monkeypatch.setenv("SCREEN_PROXY_SECRET", "s" * 32)
    monkeypatch.setenv("AGENTFORGE_BROWSER_TOKEN", TOKEN)
    policy = tmp_path / "browser.json"
    who = people[1]["alice"]
    policy.write_text(
        json.dumps(
            {
                "grants": [
                    dict(
                        tenant_id=who.tenant_id,
                        space_id=who.space_id,
                        users=["alice"],
                        origins=["*"],
                    )
                ]
            }
        )
    )
    monkeypatch.setenv("AGENTFORGE_BROWSER_POLICY_FILE", str(policy))
    running = {}
    calls = []
    hooks = []

    def exchange(self, action, *params):
        key = tuple(self.owner)
        if action == "boot" and key not in running:
            running[key] = dict(
                zip(("tenant", "space", "user", "bot"), key, strict=True),
                generation=uuid4().hex,
                port=6012,
                terminal_port=6100,
                view="v",
                control="c",
            )
        if action == "stop":
            running.pop(key, None)
        if action == "browser":
            calls.append(params[1])
            if hooks:
                hooks.pop(0)(self, params[1])
            return {"result": PAGE}
        return {"entry": running.get(key)}

    monkeypatch.setattr(Desktop, "exchange", exchange)
    gateway = model_settings()
    app = create_app(
        replace(settings, auth_mode="database", web_origin=ORIGIN),
        repository,
        model_settings=gateway,
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + people[2]["alice"]["token"]}
        bot = create_bot(client, headers)
        yield client, headers, bot, policy, calls, hooks, gateway


def submit(env):
    client, headers, bot, *_ = env
    r = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "Read public websites"})
    assert r.is_success, r.text
    return r.json()["json"]["runId"]


def execute(client, claim, **changes):
    return client.post(
        "/api/internal/browser/execute",
        headers={"Authorization": "Bearer " + TOKEN},
        json={
            "runId": claim.run.run_id,
            "attemptId": claim.attempt_id,
            "operation": {"action": "open", "url": "https://example.com/"},
            **changes,
        },
    )


def test_gateway_scope_cancel_and_service_auth(browser_env, repository):
    client, _, _, _, calls, hooks, _ = browser_env
    submit(browser_env)
    claim = repository.claim_next()
    assert claim.run.execution_config.browser_origins == ("*",)
    assert client.post("/api/internal/browser/execute", json={}).status_code == 403
    assert execute(client, claim, scope={"user_id": "bob"}).status_code == 400
    assert execute(client, claim, attemptId=str(uuid4())).status_code == 403
    assert not calls

    def callback(slot, request):
        def check():
            return client.post(
                "/api/internal/browser/check",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"operationId": request["operationId"]},
            )

        assert check().is_success
        assert slot.assignment()["generation"] == slot.generation
        repository.cancel(claim.run.scope, claim.run.run_id)
        assert check().status_code == 403

    hooks.append(callback)
    assert execute(client, claim).status_code == 403
    assert calls[0]["runId"] == claim.run.run_id and "scope" not in calls[0]
    assert execute(client, claim).status_code == 403
    assert len(calls) == 1


@pytest.mark.parametrize("revoke", ["policy", "membership", "lease"])
def test_live_revocation_denies_before_desktop(browser_env, repository, people, clock, revoke):
    client, _, _, policy, calls, _, _ = browser_env
    submit(browser_env)
    claim = repository.claim_next()
    if revoke == "policy":
        policy.write_text('{"grants":[]}')
    elif revoke == "membership":
        people[0].set_member(
            people[1]["admin"], "alice", display_name="Alice", role="member", active=False
        )
    else:
        clock.advance(1000)
    assert execute(client, claim).status_code == 403
    assert calls == []


def test_user_takeover_and_browser_exclude_each_other(browser_env, repository, people):
    client, headers, bot, _, calls, hooks, _ = browser_env
    assert login(client, people[2]["alice"]["token"]).is_success
    web = {"Origin": ORIGIN}

    def call(name):
        return rpc(client, web, "computer." + name, {"botId": bot["id"]})

    assert call("boot").is_success
    assert call("takeover").is_success
    submit(browser_env)
    claim = repository.claim_next()
    assert execute(client, claim).json()["error"] == "browser_busy"
    assert not calls
    assert call("release").is_success

    def during(slot, _):
        assert call("status").json()["json"]["controlHolder"] == "bot"
        assert call("takeover").status_code == 409
        assert call("stop").status_code == 409

    hooks.append(during)
    assert execute(client, claim).json() == PAGE
    assert call("takeover").is_success


def test_worker_browser_results_journal_and_activity(browser_env, repository, monkeypatch):
    client, _, bot, _, calls, _, gateway = browser_env
    run_id = submit(browser_env)

    def request(claim, args):
        r = execute(client, claim, operation=args)
        assert r.is_success, r.text
        return r.json()

    monkeypatch.setattr("agentforge_worker.worker.browser_request", request)
    replies = iter(
        [
            completion(
                None,
                [
                    tool_call(
                        name="browser_open", arguments=json.dumps({"url": "https://example.com/"})
                    )
                ],
            ),
            completion("Source: https://example.com/"),
        ]
    )

    def model(request):
        body = json.loads(request.content)
        names = {t["function"]["name"] for t in body["tools"]}
        assert {"browser_open", "browser_observe", "browser_follow"} <= names
        if body["messages"][-1]["role"] == "tool":
            assert "Source" in body["messages"][-1]["content"]
        return httpx.Response(200, json=next(replies))

    assert Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    ).run_once()
    scope = repository.active_claim(run_id, calls[0]["attemptId"])
    assert scope is None  # terminal attempt cannot authorize more browser actions
    from agentforge_core.models import Scope

    owner = Scope("tenant-a", "space-a", "alice")
    assert repository.get(owner, run_id).result == "Source: https://example.com/"
    journal = repository.list_tool_calls(owner, run_id)
    assert len(journal) == 1 and journal[0]["status"] == "succeeded"
    blocks = RakazoRepository(repository).snapshot(owner, bot["id"])["messages"][-1]["blocks"]
    assert any(b["kind"] == "steps" and b["steps"][0]["label"] == "browser_open@1" for b in blocks)


def test_ungranted_and_historical_runs_have_no_browser(browser_env, repository, people):
    client, _, _, _, _, _, _ = browser_env
    headers = {"Authorization": "Bearer " + people[2]["bob"]["token"]}
    bot = create_bot(client, headers)
    r = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "Hello"})
    assert r.is_success
    assert not repository.claim_next().run.execution_config.browser_origins
    assert not ExecutionSnapshot.model_validate({}).browser_origins


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "http://example.com/",
        "https://example.com:8080/",
        "https://example.com\\@evil.com/",
        "https://example.com/\nHeader:x",
    ],
)
def test_invalid_url(url):
    with pytest.raises(ValueError):
        BrowserInput(action="open", url=url)


def test_policy_public_and_exact_scope():
    from agentforge_core.models import Scope

    p = BrowserPolicy.model_validate(
        {"grants": [dict(tenant_id="t", space_id="s", users=["u"], origins=["*"])]}
    )
    assert p.origins(Scope("t", "s", "u")) == ("*",)
    assert p.origins(Scope("t", "s", "other")) == ()
    assert permitted_url("https://news.example/a", ("*",))
    assert not permitted_url("https://example.com.evil.test/", ("https://example.com",))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_fetch_blocks_nonpublic_dns_before_connection(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (ip, 443))])
    with pytest.raises(ValueError):
        browser_fetch.fetch_document("https://example.com/", ("*",))


def test_fetch_strips_credentials_and_bounds_response(monkeypatch):
    from io import BytesIO

    body = b"<html>Example</html>"
    response = BytesIO(body)
    response.status = 200
    response.getheader = lambda name, default="": {
        "Content-Type": "text/html",
        "Set-Cookie": "secret",
    }.get(name, default)
    captured = []

    class Connection:
        def __init__(self, host, ip):
            assert host == "example.com" and ip == "93.184.216.34"

        def request(self, *args, **kw):
            captured.append((args, kw))

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(browser_fetch, "public_addresses", lambda h: ["93.184.216.34"])
    monkeypatch.setattr(browser_fetch, "PinnedHTTPS", Connection)
    result = browser_fetch.fetch_document("https://example.com/path?q=1", ("*",))
    assert base64.b64decode(result["body"]) == body
    assert captured[0][0] == ("GET", "/path?q=1")
    assert not {"Cookie", "Authorization"} & captured[0][1]["headers"].keys()
    assert not any(h["name"] == "Set-Cookie" for h in result["headers"])
    response.seek(0)
    response.write(b"x" * (1024 * 1024 + 1))
    response.seek(0)
    with pytest.raises(ValueError):
        browser_fetch.fetch_document("https://example.com/", ("*",))


def test_pool_rejects_stale_cross_run_and_unknown_links_before_driver(monkeypatch):
    pool = BrowserPool()
    pool.states["gen"] = dict(identity=("run", "attempt"), page=PAGE, snapshot=PAGE["snapshot"])

    def call(run="run", attempt="attempt", snapshot=PAGE["snapshot"], link="1"):
        return pool.execute(
            "gen",
            dict(
                runId=run,
                attemptId=attempt,
                origins=["*"],
                operation=dict(action="follow", snapshot=snapshot, link=link),
            ),
        )

    assert call(run="other")["error"] == "browser_page_changed"
    assert call(attempt="old")["error"] == "browser_page_changed"
    assert call(snapshot="b" * 32)["error"] == "browser_page_changed"
    assert call(link="2")["error"] == "browser_page_changed"
    pool.forget("gen")
    assert call()["error"] == "browser_page_changed"


def test_browser_policy_is_pinned_on_duplicate_submission(browser_env, repository, people):
    client, headers, bot, policy, *_ = browser_env
    body = {"botId": bot["id"], "text": "Read sources", "clientNonce": "same-submission"}
    first = rpc(client, headers, "threads.send", body)
    assert first.is_success
    policy.write_text('{"grants":[]}')
    repeated = rpc(client, headers, "threads.send", body)
    assert repeated.json() == first.json()
    run = repository.get(people[1]["alice"], first.json()["json"]["runId"])
    assert run.execution_config.browser_origins == ("*",)
    claim = repository.claim_next()
    assert execute(client, claim).status_code == 403


def test_team_browser_excludes_other_bot_but_private_does_not(browser_env, repository, people):
    client, headers, bot, _, _, hooks, _ = browser_env
    assert login(client, people[2]["alice"]["token"]).is_success
    team = create_bot(client, headers)
    private = create_bot(client, headers, computerMode="dedicated")
    web = {"Origin": ORIGIN}

    def call(name, target):
        return rpc(client, web, "computer." + name, {"botId": target["id"]})

    assert call("boot", private).is_success
    submit(browser_env)
    claim = repository.claim_next()

    def during(slot, request):
        assert call("takeover", team).status_code == 409
        assert call("takeover", private).is_success
        assert call("release", private).is_success

    hooks.append(during)
    assert execute(client, claim).is_success
    assert call("takeover", team).is_success


def test_redirect_does_not_escape_origin_policy(monkeypatch):
    class Redirect:
        status = 302

        def getheader(self, key, default=""):
            return "https://evil.example/" if key == "Location" else default

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Redirect()

        def close(self):
            pass

    monkeypatch.setattr(browser_fetch, "public_addresses", lambda _: ["93.184.216.34"])
    monkeypatch.setattr(browser_fetch, "PinnedHTTPS", Connection)
    with pytest.raises(ValueError, match="Redirect"):
        browser_fetch.fetch_document("https://example.com/", ("https://example.com",))


def test_pool_discards_results_if_lease_is_revoked_during_driver(monkeypatch):
    from agentforge_supervisor import browser_pool

    monkeypatch.setenv("AGENTFORGE_BROWSER_TOKEN", TOKEN)
    answers = iter([200, 403])
    real_client = httpx.Client
    monkeypatch.setattr(
        browser_pool.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(next(answers)))
        ),
    )
    pool = BrowserPool()
    monkeypatch.setattr(pool, "drive", lambda *args: (PAGE, {"context": "c", "target": "t"}))
    result = pool.execute(
        "gen",
        dict(
            runId="r",
            attemptId="a",
            operationId="op",
            origins=["*"],
            operation=dict(action="open", url="https://example.com/"),
        ),
    )
    assert result == {"error": "browser_not_allowed"}
    assert pool.states["gen"]["page"] is None

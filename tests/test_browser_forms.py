"""Opt-in form tools, exact approvals and isolated continuation through the normal Worker."""

import json

import httpx
import pytest
import sqlalchemy as sa
from agentforge_api.desktop import Desktop
from agentforge_connectors.browser import BrowserPolicy, prepare_browser, prepare_browser_forms
from agentforge_core.browser import BrowserAct, BrowserFailure, BrowserInput
from agentforge_core.execution import ExecutionSnapshot
from agentforge_runtime.browser_tools import with_browser_tools
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_storage.schema import external_effects
from agentforge_supervisor import browser_pool

from tests.model_support import model_settings
from tests.test_approval_resume import SimulatedCrash, card_for
from tests.test_browser_auth import ORIGIN, login
from tests.test_orpc import create_bot, rpc
from tests.test_visible_browser import PAGE, TOKEN, execute, submit
from tests.test_visible_browser import browser_env as _browser_env
from tests.test_visible_browser import people as _people
from tests.test_workspace_file_tools import scripted_worker

browser_env = _browser_env
people = _people
FORM_PAGE = {**PAGE, "elements": [], "truncated": False}


def act_args(text="公司 AI 平台"):
    return dict(
        snapshot=PAGE["snapshot"],
        url=PAGE["url"],
        actions=[dict(kind="fill", ref="f0e0", text=text)],
    )


@pytest.fixture
def forms_env(browser_env, monkeypatch):
    client, _, _, policy, *_ = browser_env
    data = json.loads(policy.read_text())
    data["grants"].append({**data["grants"][0], "origins": ["https://example.com"], "forms": True})
    policy.write_text(json.dumps(data))
    previous = Desktop.exchange

    def exchange(self, action, *params):
        reply = previous(self, action, *params)
        if action == "browser" and params[1]["operation"]["action"] in {"snapshot", "act"}:
            return {"result": FORM_PAGE}
        return reply

    monkeypatch.setattr(Desktop, "exchange", exchange)

    def request(claim, arguments):
        reply = execute(client, claim, operation=arguments)
        if not reply.is_success:
            raise BrowserFailure(reply.json()["error"])
        return reply.json()

    monkeypatch.setattr("agentforge_worker.worker.browser_request", request)
    return browser_env


def answer(env, repo, owner, rid, decision="allow", headers=None):
    client, default_headers, bot, *_ = env
    card = card_for(repo, repo.get(owner, rid))
    return rpc(
        client,
        headers or default_headers,
        "threads.answer",
        dict(
            botId=bot["id"],
            runId=rid,
            messageId=card["message_id"],
            answer=decision,
        ),
    )


def effect_status(repo, rid):
    with repo.engine.connect() as conn:
        return conn.execute(
            sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
        ).scalar_one()


def test_forms_opt_in_and_tool_budget(forms_env, people):
    owner = people[1]["alice"]
    old = ExecutionSnapshot(model=model_settings().snapshot(), browser_origins=("*",))
    assert old.browser_form_origins == () and "browser_act@1" not in old.tool_references
    new = prepare_browser_forms(owner, prepare_browser(owner, old))
    assert new.browser_form_origins == ("https://example.com",)
    assert "browser_act@1" in new.approval_tools
    full = ExecutionSnapshot(model=old.model, tools=tuple(f"tool_{i}@1" for i in range(10)))
    result = prepare_browser_forms(owner, prepare_browser(owner, full))
    assert len(result.tool_references) == 16 and not result.browser_form_origins
    assert not prepare_browser(people[1]["bob"], old).browser_form_origins
    assert not prepare_browser(owner, ExecutionSnapshot()).browser_form_origins
    with pytest.raises(ValueError):
        BrowserPolicy.model_validate(
            {
                "grants": [
                    dict(
                        tenant_id="t",
                        space_id="s",
                        users=["u"],
                        origins=["*"],
                        forms=True,
                    )
                ]
            }
        )
    for changes in [
        dict(approval_tools=()),
        dict(browser_reading=False),
        dict(browser_form_origins=("*",)),
    ]:
        with pytest.raises(ValueError):
            ExecutionSnapshot.model_validate({**new.model_dump(), **changes})


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [dict(kind="fill", ref="f0e0")],
        [dict(kind="click", ref="f0e0", text="x")],
        [dict(kind="fill", ref="#password", text="x")],
        [dict(kind="click", ref="f0e1"), dict(kind="fill", ref="f0e0", text="x")],
        [dict(kind="fill", ref="f0e0", text="x" * 1001)],
        [dict(kind="type", ref="f0e0", text="x" * 700)] * 3,
        [dict(kind="fill", ref="f0e0", text="\x00")],
    ],
)
def test_invalid_form_actions(actions):
    with pytest.raises(ValueError):
        BrowserAct(**{**act_args(), "actions": actions})
    with pytest.raises(ValueError):
        BrowserInput(action="observe", actions=actions)


def test_service_cannot_execute_unapproved_running_tool(forms_env, repository):
    submit(forms_env)
    claim = repository.claim_next()
    registry = with_browser_tools(BUILTIN_TOOLS, lambda _: None, ("*",), reading=True, forms=True)
    executor = ToolExecutor(
        ("browser_act@1",),
        registry=registry,
        allowed=("browser_act@1",),
        write_allowed=("browser_act@1",),
    )
    prepared = executor.prepare("call", "browser_act", json.dumps(act_args()))
    repository.start_tool(claim, prepared.invocation)
    reply = execute(forms_env[0], claim, operation={"action": "act", **act_args()})
    assert reply.status_code == 403 and not forms_env[4]


def test_worker_approval_exact_arguments_and_new_attempt(forms_env, repository, people):
    owner = people[1]["alice"]
    rid = submit(forms_env)
    # More than 8 KiB even in UTF-8: exercise the bounded internal API with legal Unicode input.
    args = act_args("😀" * 1000)
    args["actions"].append(dict(kind="fill", ref="f0e1", text="中" * 1000))
    args["url"] += "?query=" + "a" * 1500
    worker, requests = scripted_worker(
        repository,
        [
            ("browser_snapshot", {}),
            ("browser_act", args),
        ],
    )
    worker.run_once()
    assert repository.get(owner, rid).status.value == "waiting_input"
    assert [c["operation"]["action"] for c in forms_env[4]] == ["snapshot"]
    assert len(requests) == 2
    other = {"Authorization": "Bearer " + people[2]["bob"]["token"]}
    assert not answer(forms_env, repository, owner, rid, headers=other).is_success
    assert answer(forms_env, repository, owner, rid).is_success

    def during(slot, request):
        claim = repository.active_claim(rid, request["attemptId"])
        forged = act_args("changed after approval")
        assert (
            execute(forms_env[0], claim, operation={"action": "act", **forged}).status_code == 403
        )
        assert effect_status(repository, rid) == "dispatched"

    forms_env[5].append(during)
    assert worker.run_once()
    assert repository.get(owner, rid).status.value == "succeeded"
    assert effect_status(repository, rid) == "succeeded"
    assert len(requests) == 3  # the approved arguments were not generated again
    calls = forms_env[4]
    assert len(calls) == 2 and calls[0]["attemptId"] != calls[1]["attemptId"]
    actual = calls[1]["operation"]
    assert BrowserAct.model_validate({k: actual[k] for k in args}).actions[0].text == "😀" * 1000
    assert all(c["status"] == "succeeded" for c in repository.list_tool_calls(owner, rid))


@pytest.mark.parametrize("stop", ["deny", "cancel", "policy", "takeover"])
def test_pending_form_does_not_execute_after_refusal(forms_env, repository, people, stop):
    owner = people[1]["alice"]
    rid = submit(forms_env)
    worker, _ = scripted_worker(repository, [("browser_act", act_args())])
    worker.run_once()
    if stop == "cancel":
        repository.cancel(owner, rid)
    else:
        assert answer(
            forms_env, repository, owner, rid, "deny" if stop == "deny" else "allow"
        ).is_success
        if stop == "policy":
            data = json.loads(forms_env[3].read_text())
            data["grants"].pop()  # read access remains, form access was revoked
            forms_env[3].write_text(json.dumps(data))
        elif stop == "takeover":
            assert login(forms_env[0], people[2]["alice"]["token"]).is_success
            for op in ("boot", "takeover"):
                assert rpc(
                    forms_env[0],
                    {"Origin": ORIGIN},
                    "computer." + op,
                    {"botId": forms_env[2]["id"]},
                ).is_success
    worker.run_once()
    assert not forms_env[4]
    assert repository.get(owner, rid).status.value == (
        "succeeded" if stop == "deny" else "cancelled" if stop == "cancel" else "failed"
    )


@pytest.mark.parametrize("failure", ["browser_page_changed", "browser_action_unsupported"])
def test_definite_preflight_refusal_keeps_failed_audit_and_refreshes(
    forms_env, repository, people, monkeypatch, failure
):
    original = Desktop.exchange

    def refuse(self, action, *params):
        if action == "browser" and params[1]["operation"]["action"] == "act":
            return {"error": failure}
        return original(self, action, *params)

    monkeypatch.setattr(Desktop, "exchange", refuse)
    owner = people[1]["alice"]
    rid = submit(forms_env)
    worker, requests = scripted_worker(
        repository,
        [
            ("browser_act", act_args()),
            ("browser_snapshot", {}),
        ],
    )
    worker.run_once()
    assert answer(forms_env, repository, owner, rid).is_success
    worker.run_once()
    assert repository.get(owner, rid).status.value == "succeeded"
    assert effect_status(repository, rid) == "failed"
    journal = repository.list_tool_calls(owner, rid)
    assert journal[0]["status"] == "failed" and journal[0]["error_code"] == failure
    assert json.loads(requests[1]["messages"][-1]["content"])["error"] == failure


def test_unknown_dispatch_is_not_replayed_after_crash(
    forms_env, repository, people, monkeypatch, clock
):
    import agentforge_worker.worker as module

    original = module.browser_request

    def crash(claim, arguments):
        original(claim, arguments)
        raise SimulatedCrash()

    monkeypatch.setattr(module, "browser_request", crash)
    owner = people[1]["alice"]
    rid = submit(forms_env)
    worker, _ = scripted_worker(repository, [("browser_act", act_args())])
    worker.run_once()
    assert answer(forms_env, repository, owner, rid).is_success
    with pytest.raises(SimulatedCrash):
        worker.run_once()
    assert effect_status(repository, rid) == "dispatched"
    clock.advance(61)
    assert worker.run_once()
    assert repository.get(owner, rid).error_code == "effect_uncertain"
    assert len(forms_env[4]) == 1


def test_old_and_other_user_runs_cannot_gain_forms(browser_env, repository, people):
    submit(browser_env)
    claim = repository.claim_next()
    assert execute(browser_env[0], claim, operation={"action": "snapshot"}).status_code == 403
    assert (
        execute(browser_env[0], claim, operation={"action": "act", **act_args()}).status_code == 403
    )
    headers = {"Authorization": "Bearer " + people[2]["bob"]["token"]}
    bot = create_bot(browser_env[0], headers)
    assert rpc(
        browser_env[0], headers, "threads.send", {"botId": bot["id"], "text": "Hi"}
    ).is_success
    bob = repository.claim_next()
    assert not bob.run.execution_config.browser_form_origins
    assert execute(browser_env[0], bob, operation={"action": "snapshot"}).status_code == 403
    assert (
        execute(
            browser_env[0],
            claim,
            attemptId=bob.attempt_id,
            operation={"action": "act", **act_args()},
        ).status_code
        == 403
    )


def test_pool_approval_attempt_continuation_and_cross_run_isolation(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_BROWSER_TOKEN", TOKEN)
    client = httpx.Client
    monkeypatch.setattr(
        browser_pool.httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    pool = browser_pool.BrowserPool()
    old_state = {"context": "guest", "target": "target"}
    pool.states["gen"] = dict(
        identity=("r", "a"), state=old_state, page=FORM_PAGE, snapshot=PAGE["snapshot"]
    )

    def request(run, attempt, action, **data):
        return pool.execute(
            "gen",
            dict(
                runId=run,
                attemptId=attempt,
                operationId="op",
                origins=["*"],
                formOrigins=["https://example.com"],
                operation=dict(action=action, **data),
            ),
        )

    for run, attempt in [("other", "b"), ("r", "b")]:
        assert request(run, attempt, "snapshot")["error"] == "browser_page_changed"
    assert request("other", "b", "act", **act_args())["error"] == "browser_page_changed"
    configs = []

    def drive(gen, config, *args):
        configs.append(config)
        page = PAGE if config["operation"]["action"] == "open" else FORM_PAGE
        return {k: v for k, v in page.items() if k != "snapshot"}, old_state

    monkeypatch.setattr(pool, "drive", drive)
    done = request("r", "b", "act", **act_args())
    assert "result" in done and configs[0]["state"] == old_state
    assert request("r", "b", "act", **act_args())["error"] == "browser_page_changed"
    assert (
        request("r", "b", "act", **{**act_args(), "url": "https://evil.example/"})["error"]
        == "browser_not_allowed"
    )
    assert "result" in request("other", "c", "open", url=PAGE["url"])
    assert configs[-1]["state"] is None and configs[-1]["closeContext"] == "guest"


def test_forms_do_not_displace_existing_workspace_tools(
    forms_env, repository, people, monkeypatch, tmp_path
):
    from tests.test_python_execution import IMAGE

    who = people[1]["alice"]
    policy = tmp_path / "python-workspace.json"
    policy.write_text(
        json.dumps(
            {
                "grants": [
                    dict(
                        tenant_id=who.tenant_id,
                        space_id=who.space_id,
                        users=[who.user_id],
                    )
                ]
            }
        )
    )
    for key in ("AGENTFORGE_PYTHON_POLICY_FILE", "AGENTFORGE_WORKSPACE_POLICY_FILE"):
        monkeypatch.setenv(key, str(policy))
    monkeypatch.setenv("AGENTFORGE_PYTHON_IMAGE", IMAGE)
    monkeypatch.setenv("AGENTFORGE_MCP_COMPUTER_TOKEN", "c" * 32)
    monkeypatch.setenv("AGENTFORGE_WEB_ENABLED", "true")
    rid = submit(forms_env)
    config = repository.get(who, rid).execution_config
    assert len(config.tool_references) == 16
    assert config.workspace_file_tools and config.python_continuity
    assert not config.browser_form_origins


@pytest.mark.parametrize("stop", ["policy", "membership", "cancel", "lease"])
def test_form_callback_rechecks_current_authority(forms_env, repository, people, clock, stop):
    owner = people[1]["alice"]
    rid = submit(forms_env)
    worker, _ = scripted_worker(repository, [("browser_act", act_args())])
    worker.run_once()
    assert answer(forms_env, repository, owner, rid).is_success

    def revoke(slot, request):
        def check():
            return forms_env[0].post(
                "/api/internal/browser/check",
                headers={
                    "Authorization": "Bearer " + TOKEN,
                },
                json={"operationId": request["operationId"]},
            )

        assert check().is_success
        if stop == "policy":
            data = json.loads(forms_env[3].read_text())
            data["grants"].pop()
            forms_env[3].write_text(json.dumps(data))
        elif stop == "cancel":
            repository.cancel(owner, rid)
        elif stop == "membership":
            people[0].set_member(
                people[1]["admin"], "alice", display_name="Alice", role="member", active=False
            )
        else:
            clock.advance(1000)
        assert check().status_code == 403

    forms_env[5].append(revoke)
    worker.run_once()
    assert effect_status(repository, rid) == "dispatched"  # success cannot be confirmed


def test_large_form_snapshot_is_truncated_within_tool_result_budget(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_BROWSER_TOKEN", TOKEN)
    client = httpx.Client
    monkeypatch.setattr(
        browser_pool.httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    pool = browser_pool.BrowserPool()
    pool.states["gen"] = dict(identity=("r", "a"), state={"context": "guest"}, page=PAGE)
    large = {
        **FORM_PAGE,
        "text": "中\n" * 2000,
        "elements": [
            dict(
                ref=f"f{i // 40}e{i % 40}",
                role="textbox",
                name="x" * 120,
                value="中" * 1000,
                form_url="https://example.com/" + "x" * 1900,
            )
            for i in range(80)
        ],
    }
    monkeypatch.setattr(pool, "drive", lambda *args: (large, {"context": "guest"}))
    result = pool.execute(
        "gen",
        dict(
            runId="r",
            attemptId="a",
            operationId="op",
            origins=["*"],
            formOrigins=["https://example.com"],
            operation={"action": "snapshot"},
        ),
    )
    page = result["result"]
    assert page["truncated"] and 0 < len(page["elements"]) < 80
    assert len(json.dumps(page, ensure_ascii=False)) <= 19000

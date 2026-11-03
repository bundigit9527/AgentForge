import json
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from agentforge_api.desktop import Desktop
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_storage.conversations import ConversationRepository


def test_desktop_control_and_revocation(repository, scope, tmp_path):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_runtime.config import ModelSettings

    agent = ConversationRepository(repository.engine, repository.clock).create_agent(
        scope, "Dedicated desktop", ExecutionSnapshot(model=ModelSettings().snapshot())
    )
    bot = agent["agent_id"]
    path = tmp_path / "desktop.json"
    entry = dict(
        tenant=scope.tenant_id,
        space=scope.space_id,
        user=scope.user_id,
        bot=bot,
        generation="one",
        port=23456,
        view="read",
        control="write",
    )
    path.write_text(json.dumps(entry))
    sessions = {"first": (scope, None), "second": (scope, None)}

    class Browser:
        def resolve(self, cookie):
            if cookie not in sessions:
                raise PermissionDenied()
            return sessions[cookie]

    desktop = Desktop(path, repository, Browser())

    def call(action, cookie="first"):
        return desktop.handle(action, scope, bot, cookie)

    def socket(cookie="first"):
        return urlsplit(call("screenUrl", cookie)["url"]).path.replace("embed.html", "websockify")

    view = socket()
    assert desktop.resolve(view)["path"] == "/websockify?token=read"
    assert call("takeover")["leaseId"]
    control = socket()
    assert desktop.resolve(control)["interactive"]
    with pytest.raises(RunConflict):
        call("takeover", "second")
    with pytest.raises(PermissionDenied):
        call("release", "second")
    assert not desktop.resolve(socket("second"))["interactive"]
    with pytest.raises(RunNotFound):
        desktop.handle("boot", replace(scope, user_id="stranger"), bot, "first")
    with pytest.raises(PermissionDenied):
        desktop.resolve(view.replace("/view/", "/control/"))
    call("release")
    with pytest.raises(PermissionDenied):
        desktop.resolve(control)
    assert not desktop.resolve(view)["interactive"]
    call("takeover")
    with pytest.raises(PermissionDenied):
        desktop.resolve(control)
    sessions.pop("first")
    with pytest.raises(PermissionDenied):
        desktop.resolve(view)
    prior = socket("second")
    path.write_text(json.dumps({**entry, "generation": "two"}))
    with pytest.raises(PermissionDenied):
        desktop.resolve(prior)

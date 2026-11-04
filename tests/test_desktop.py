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
    path.write_text(json.dumps({**entry, "terminal_port": 6789}))
    term = call("terminalUrl")["url"].rsplit("/", 1)[0] + "/websockify"
    assert desktop.resolve(term)["port"] == 6789
    desktop.lease_end = 0
    with pytest.raises(PermissionDenied):
        desktop.resolve(term)
    call("takeover")
    term = call("terminalUrl")["url"].rsplit("/", 1)[0] + "/websockify"
    sessions.pop("first")
    with pytest.raises(PermissionDenied):
        desktop.resolve(term)
    with pytest.raises(PermissionDenied):
        desktop.resolve(view)
    prior = socket("second")
    path.write_text(json.dumps({**entry, "generation": "two"}))
    with pytest.raises(PermissionDenied):
        desktop.resolve(prior)


def test_managed_desktops_isolate_accounts_and_invalidate_old_links(repository, scope, tmp_path):
    from uuid import uuid4

    from agentforge_api.desktop import DesktopDirectory
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_runtime.config import ModelSettings

    other = replace(scope, user_id="other")
    sessions = {"a": (scope, None), "b": (other, None)}

    class Browser:
        def resolve(self, cookie):
            return sessions[cookie]

    class TestSlot(Desktop):
        entry = None

        def exchange(self, action, generation, request):
            assert generation == self.entry["generation"]
            return {"result": {"ok": True}}

        def lifecycle(self, action):
            if action == "boot" and self.entry is None:
                self.entry = dict(zip(("tenant", "space", "user", "bot"), self.owner, strict=True))
                self.entry.update(generation=uuid4().hex, port=12345, view="r", control="w")
            if action == "stop":
                self.entry = None
            return self.entry

    directory = DesktopDirectory([])
    conversations = ConversationRepository(repository.engine, repository.clock)
    for user in (scope, other):
        bot = conversations.create_agent(
            user, "Private desktop", ExecutionSnapshot(model=ModelSettings().snapshot())
        )["agent_id"]
        directory.desktops.append(
            TestSlot(
                tmp_path / bot,
                repository,
                Browser(),
                [user.tenant_id, user.space_id, user.user_id, bot],
            )
        )
    a, b = directory.desktops
    bot_a, bot_b = a.owner[-1], b.owner[-1]
    assert directory.select(other, bot_a) is None
    with pytest.raises(RunNotFound):
        a.handle("boot", other, bot_a, "b")
    with pytest.raises(PermissionDenied):
        a.handle("boot", scope, bot_a, "b")
    assert a.handle("status", scope, bot_a, "a")["state"] == "stopped"
    for slot, user, bot, cookie in ((a, scope, bot_a, "a"), (b, other, bot_b, "b")):
        assert slot.handle("boot", user, bot, cookie)["state"] == "running"
        generation = slot.entry["generation"]
        slot.handle("boot", user, bot, cookie)
        assert slot.entry["generation"] == generation
        assert slot.handle("takeover", user, bot, cookie)["leaseId"]
    req = {"path": "mine.txt", "contentBase64": ""}
    assert a.files("uploadFile", scope, bot_a, "a", req) == {"ok": True}
    with pytest.raises(RunNotFound):
        a.files("downloadFile", other, bot_a, "b", req)
    sessions["another"] = (scope, None)
    with pytest.raises(PermissionDenied):
        a.files("uploadFile", scope, bot_a, "another", req)
    url_a = urlsplit(a.handle("screenUrl", scope, bot_a, "a")["url"]).path
    url_b = urlsplit(b.handle("screenUrl", other, bot_b, "b")["url"]).path
    assert directory.resolve(url_a)["interactive"]
    for _ in range(2):
        assert a.handle("stop", scope, bot_a, "a")["state"] == "stopped"
    a.handle("boot", scope, bot_a, "a")
    with pytest.raises(PermissionDenied):
        directory.resolve(url_a)
    with pytest.raises(PermissionDenied):
        a.files("uploadFile", scope, bot_a, "a", req)
    assert directory.resolve(url_b)["interactive"]


def test_workspace_access_and_atomic_uploads(tmp_path):
    import base64
    import os

    from agentforge_supervisor.workspace_files import operate

    root = tmp_path / "work"
    root.mkdir()
    (root / "资料").mkdir()
    (root / "external").symlink_to(tmp_path)
    os.mkfifo(root / "pipe")
    name = "资料/报告.txt"
    payload = base64.b64encode("中文".encode()).decode()
    assert operate("uploadFile", {"path": name, "contentBase64": payload}, root) == {"ok": True}
    assert operate("files", {"path": "/"}, root) == [{"path": "资料", "kind": "dir", "size": 0}]
    assert operate("files", {"path": "资料"}, root)[0]["path"] == name
    assert operate("readFile", {"path": name}, root)["content"] == "中文"
    assert operate("downloadFile", {"path": name}, root)["contentBase64"] == payload
    for path in ("../escape", "/etc/passwd", "external/work", "pipe"):
        with pytest.raises((ValueError, OSError)):
            operate("readFile", {"path": path}, root)
    with pytest.raises((ValueError, OSError)):
        operate("uploadFile", {"path": "external/leak", "contentBase64": payload}, root)
    assert not (tmp_path / "leak").exists()
    with pytest.raises(ValueError):
        operate("uploadFile", {"path": name, "contentBase64": "invalid!"}, root)
    assert (root / name).read_text() == "中文"


def test_terminal_stream_frames_split_and_coalesced():
    import struct

    from agentforge_supervisor.terminal_pty import frames

    data = bytearray(b"\x00\x00")
    assert list(frames(data)) == []
    data.extend(b"\x00\x00\x02hi" + struct.pack(">BIHH", 1, 4, 120, 32))
    assert list(frames(data)) == [(0, b"hi"), (1, struct.pack(">HH", 120, 32))]
    assert not data
    for invalid in (
        struct.pack(">BI", 3, 0),
        struct.pack(">BI", 1, 100),
        struct.pack(">BI", 0, 65537),
    ):
        with pytest.raises(ValueError):
            list(frames(bytearray(invalid)))

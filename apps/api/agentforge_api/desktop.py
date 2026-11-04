"""Original Rakazo screen protocol for an explicitly assigned local Docker desktop."""

import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from agentforge_core.desktop import slot_manifest
from agentforge_core.identity import AuthenticationFailed, PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound, Scope
from agentforge_storage.schema import agents, runs
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

SCREEN_PATH = re.compile(
    r"/novnc/session/(view|control)/([A-Za-z0-9_-]{43})/([\w./-]+)(?:\?[^#\s]*)?",
    re.ASCII,
)
ASSET_PATH = re.compile(
    r"(?:core|vendor|app|include)/[A-Za-z0-9_./-]+|embed\.html|clipboard-bridge\.js|mobile-keyboard\.js|websockify"
)


TEAM_ID = str(UUID(int=0))


class ReaderRequest(BaseModel):
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    release: str | None = Field(default=None, max_length=64)


class TerminalAccess(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")


class TargetRequest(BaseModel):
    path: str = Field(max_length=4096)


@dataclass
class Ticket:
    cookie: str
    scope: Scope
    generation: str
    bot: str
    policy: str
    lease: str | None
    expires: float
    terminal: bool = False


class DesktopFileInvalid(Exception):
    pass


class Desktop:
    def __init__(self, manifest, store, browser, owner=None):
        self.owner = owner
        self.manifest = Path(manifest)
        self.store = store
        self.browser = browser
        self.lock = threading.RLock()
        self.tickets = {}
        self.controller = None
        self.control_bot = None
        self.pool = None
        self.lease = None
        self.lease_end = 0
        self.generation = None
        self.reader_id = None
        self.reader_end = 0

    @property
    def team(self):
        return bool(self.owner and self.owner[2:] == ["", TEAM_ID])

    def matches(self, scope, bot):
        return self.owner[:2] == [scope.tenant_id, scope.space_id] and (
            self.team or self.owner[2:] == [scope.user_id, bot]
        )

    def controls(self, cookie, bot):
        return bool(cookie and self.controller == cookie and self.control_bot == bot)

    def exchange(self, action, *params):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(90 if action == "boot" else 35)
                sock.connect(str(self.pool or (str(self.manifest) + ".sock")))
                sock.sendall(json.dumps([action, self.owner, *params]).encode() + b"\n")
                with sock.makefile("rb") as reader:
                    return json.loads(reader.readline(14_100_000 if params else 8192))
        except (OSError, ValueError) as exc:
            raise RunConflict("Desktop Supervisor unavailable") from exc

    def lifecycle(self, action):
        try:
            reply = self.exchange(action)
            if reply.get("error") == "pool_full":
                raise RunConflict("Desktop pool is full")
            entry = reply["entry"]
            if entry and [entry[k] for k in ("tenant", "space", "user", "bot")] != self.owner:
                raise ValueError("Unexpected desktop owner")
            return entry
        except (OSError, KeyError, ValueError) as exc:
            raise RunConflict("Desktop Supervisor unavailable") from exc

    def files(self, action, scope, bot, cookie, request):
        with self.lock:
            if not self.matches(scope, bot):
                raise RunNotFound()
            writing = action == "uploadFile"
            self.authorize(scope, bot, write=writing)
            if not cookie or self.browser.resolve(cookie)[0] != scope:
                raise PermissionDenied()
            entry = self.assignment()
            self.sync(entry)
            if not entry:
                raise RunConflict("Start the computer first")
            if writing and not self.controls(cookie, bot):
                raise PermissionDenied()
            reply = self.exchange(
                action,
                entry["generation"],
                {**request, "workspaceBot": bot} if self.team else request,
            )
            if "result" in reply:
                return reply["result"]
            if reply.get("error") == "file_missing":
                raise RunNotFound()
            if reply.get("error") == "file_invalid":
                raise DesktopFileInvalid()
            raise RunConflict("Desktop workspace unavailable")

    def assignment(self):
        if self.owner:
            return self.lifecycle("status")
        try:
            return json.loads(self.manifest.read_text())
        except FileNotFoundError:
            return None

    @staticmethod
    def assigned(entry, scope, bot=None):
        return bool(
            entry
            and (entry["tenant"], entry["space"], entry["user"])
            == (scope.tenant_id, scope.space_id, scope.user_id)
            and (bot is None or bot == entry["bot"])
        )

    def authorize(self, scope, bot, write=False):
        with self.store.engine.connect() as conn:
            self.store.authorization.require(conn, scope, write=write)
            self.store.authorization.visibility(conn, scope, "agent", bot)
            mode = conn.execute(
                sa.select(agents.c.computer_mode).where(agents.c.agent_id == bot)
            ).scalar_one()
            if mode != ("team" if self.team else "dedicated"):
                raise RunNotFound()

    def sync(self, entry):
        generation = entry["generation"] if entry else None
        if generation != self.generation or self.lease_end <= time.time():
            self.controller = self.lease = None
        elif self.controller:
            try:
                scope, _ = self.browser.resolve(self.controller)
                self.authorize(scope, self.control_bot, True)
            except (AuthenticationFailed, PermissionDenied, RunNotFound):
                self.controller = self.lease = None
        if self.reader_end <= time.monotonic() or generation != self.generation:
            self.reader_id = None
        if generation != self.generation:
            self.tickets.clear()
            self.generation = generation

    def status(self, entry, bot, cookie):
        self.sync(entry)
        controlled = self.controller is not None
        return dict(
            botId=bot,
            mode="team" if self.team else "dedicated",
            kind="docker",
            state="running" if entry else "stopped",
            controlHolder="user"
            if controlled and self.controls(cookie, bot)
            else ("bot" if self.reader_id else "none"),
            controlBotId=bot if controlled and self.controls(cookie, bot) else None,
            takeoverRequested=False,
            screenAvailable=bool(entry),
            screenWidth=1280,
            screenHeight=800,
            homeRevision=None,
            busyBotName=None,
            canUpdate=False,
            terminalAvailable=bool(entry and entry.get("terminal_port")),
        )

    def snapshot(self, scope, bot, cookie, snap):
        with self.lock:
            if self.owner:
                self.authorize(scope, bot)
            try:
                entry = self.assignment()
            except RunConflict:
                snap["computer"] = {**self.status(None, bot, cookie), "state": "error"}
                return snap
            if self.owner or self.assigned(entry, scope, bot):
                self.authorize(scope, bot)
                snap["computer"] = self.status(entry, bot, cookie)
        return snap

    def handle(self, action, scope, bot, cookie):
        with self.lock:
            if self.owner:
                if not self.matches(scope, bot):
                    raise RunNotFound()
                self.authorize(scope, bot, action in ("boot", "stop", "takeover"))
                if action in ("boot", "stop"):
                    if not cookie or self.browser.resolve(cookie)[0] != scope:
                        raise PermissionDenied()
                    if action == "stop" and self.team:
                        self.sync(self.assignment())
                        if self.controller or self.reader_id:
                            raise RunConflict("Release the computer first")
                        with self.store.engine.connect() as conn:
                            active = conn.execute(
                                sa.select(runs.c.run_id)
                                .join(agents, agents.c.agent_id == runs.c.agent_id)
                                .where(
                                    agents.c.tenant_id == scope.tenant_id,
                                    agents.c.space_id == scope.space_id,
                                    agents.c.computer_mode == "team",
                                    runs.c.status.in_(("queued", "running", "waiting_input")),
                                )
                                .limit(1)
                            ).first()
                            if active:
                                raise RunConflict("Stop the bot first")
                    entry = self.lifecycle(action)
                    return self.status(entry, bot, cookie)
            entry = self.assignment()
            if self.owner and action == "status":
                return self.status(entry, bot, cookie)
            if not entry or (not self.team and not self.assigned(entry, scope, bot)):
                raise RunNotFound()
            self.authorize(scope, bot, action == "takeover")
            self.sync(entry)
            if action in ("status", "boot"):
                return self.status(entry, bot, cookie)
            if not cookie or self.browser.resolve(cookie)[0] != scope:
                raise PermissionDenied()
            if action == "takeover":
                if self.reader_id:
                    raise RunConflict("财经检索正在使用浏览器，请在当前页面加载结束后接管")
                if self.controller and not self.controls(cookie, bot):
                    raise RunConflict("Desktop is controlled by another login session")
                self.control_bot = bot
                self.controller = cookie
                self.lease = self.lease or str(uuid4())
                self.lease_end = time.time() + 300
                return {
                    "leaseId": self.lease,
                    "expiresAt": datetime.fromtimestamp(self.lease_end, UTC).isoformat(),
                }
            if action == "release":
                if self.controller and not self.controls(cookie, bot):
                    raise PermissionDenied()
                self.controller = self.lease = None
                return {"ok": True}
            if action == "heartbeat":
                if self.controls(cookie, bot):
                    self.authorize(scope, bot, True)
                    self.lease_end = time.time() + 300
                return {"ok": True}
            terminal = action == "terminalUrl"
            if terminal:
                if not self.controls(cookie, bot):
                    raise PermissionDenied()
                self.authorize(scope, bot, True)
                if not entry.get("terminal_port"):
                    return {"url": None}
            elif action != "screenUrl":
                raise ValueError("Unsupported desktop action")
            policy = "control" if self.controls(cookie, bot) else "view"
            lease = self.lease if policy == "control" else None
            now = time.time()
            self.tickets = {
                k: v
                for k, v in self.tickets.items()
                if v.expires > now and (not v.lease or v.lease == self.lease)
            }
            key = next(
                (
                    k
                    for k, v in self.tickets.items()
                    if v.cookie == cookie
                    and v.bot == bot
                    and v.terminal == terminal
                    and v.policy == policy
                    and v.lease == lease
                    and v.expires > now + 30
                ),
                None,
            )
            if key is None:
                key = secrets.token_urlsafe(32)
                self.tickets[key] = Ticket(
                    cookie, scope, self.generation, bot, policy, lease, now + 3600, terminal
                )
            prefix = f"/novnc/session/{policy}/{key}"
            if terminal:
                return {"url": f"{prefix}/terminal"}
            view_only = "false" if lease else "true"
            return {"url": f"{prefix}/embed.html?view_only={view_only}&path=websockify"}

    def verify(self, key):
        with self.lock:
            entry = self.assignment()
            self.sync(entry)
            grant = self.tickets.get(key)
            if not grant or grant.expires <= time.time():
                raise PermissionDenied()
            if (
                not entry
                or (not self.team and not self.assigned(entry, grant.scope, grant.bot))
                or self.browser.resolve(grant.cookie)[0] != grant.scope
            ):
                raise PermissionDenied()
            control = grant.policy == "control"
            if control and (
                not self.lease
                or grant.lease != self.lease
                or not self.controls(grant.cookie, grant.bot)
            ):
                raise PermissionDenied()
            self.authorize(grant.scope, grant.bot, control)
            return entry, grant

    def resolve(self, path):
        match = SCREEN_PATH.fullmatch(path)
        if not match:
            raise PermissionDenied()
        policy, key, asset = match.groups()
        if not ASSET_PATH.fullmatch(asset) or any(p in ("", ".", "..") for p in asset.split("/")):
            raise PermissionDenied()
        with self.lock:
            entry, grant = self.verify(key)
            if grant.policy != policy:
                raise PermissionDenied()
            if grant.terminal:
                if asset != "websockify" or not entry.get("terminal_port"):
                    raise PermissionDenied()
                return dict(
                    protocol="http:",
                    hostname="127.0.0.1",
                    port=entry["terminal_port"],
                    path=f"/terminal/{key}",
                    interactive=True,
                )
            control = policy == "control"
            suffix = "?token=" + entry[policy] if asset == "websockify" else ""
            return dict(
                protocol="http:",
                hostname="127.0.0.1",
                port=entry["port"],
                path="/" + asset + suffix,
                interactive=control,
            )


class DesktopDirectory:
    def __init__(self, desktops, pool=None, store=None, browser=None):
        self.desktops = desktops
        self.pool, self.store, self.browser = pool, store, browser
        self.lock = threading.RLock()
        self.dynamic = {}

    def all_desktops(self):
        with self.lock:
            return [*self.desktops, *self.dynamic.values()]

    def switch(self, service, scope, bot, mode):
        with self.lock:
            slot = self.select(scope, bot)
            if not self.pool and mode == "team":
                raise RunConflict("Team Computer requires a configured desktop pool")
            if slot is None:
                return service.set_computer(scope, bot, mode)
            with slot.lock:
                previous = "team" if slot.team else "dedicated"
                if mode != previous:
                    entry = slot.assignment()
                    slot.sync(entry)
                    if (slot.controller and slot.control_bot == bot) or slot.reader_id:
                        raise RunConflict("Release the computer first")
                    if entry and not slot.team:
                        raise RunConflict("Stop the private computer first")
                result = service.set_computer(scope, bot, mode)
                if mode != previous:
                    slot.tickets = {k: v for k, v in slot.tickets.items() if v.bot != bot}
                return result

    def select(self, scope, bot=None):
        if self.pool:
            if bot is None:
                return True
            with self.store.engine.connect() as conn:
                mode = conn.execute(
                    sa.select(agents.c.computer_mode).where(
                        agents.c.agent_id == bot,
                        agents.c.tenant_id == scope.tenant_id,
                        agents.c.space_id == scope.space_id,
                        agents.c.user_id == scope.user_id,
                    )
                ).scalar_one_or_none()
            if not mode:
                return None
            key = (
                (scope.tenant_id, scope.space_id, "", TEAM_ID)
                if mode == "team"
                else (scope.tenant_id, scope.space_id, scope.user_id, bot)
            )
            with self.lock:
                if key not in self.dynamic:
                    desktop = Desktop(
                        slot_manifest(self.pool.parent, key), self.store, self.browser, list(key)
                    )
                    desktop.pool = self.pool
                    self.dynamic[key] = desktop
                return self.dynamic[key]
        for desktop in self.desktops:
            owner = desktop.owner
            if owner:
                if owner[:3] == [scope.tenant_id, scope.space_id, scope.user_id] and (
                    bot is None or owner[3] == bot
                ):
                    return desktop
            elif desktop.assigned(desktop.assignment(), scope, bot):
                return desktop
        return None

    def reader(self, generation):
        for desktop in self.all_desktops():
            if desktop.team:
                continue
            # Use the private local manifest only as an index, then verify the live assignment.
            try:
                entry = json.loads(desktop.manifest.read_text())
            except FileNotFoundError:
                continue
            if entry["generation"] == generation:
                return desktop
        raise HTTPException(404)

    def resolve(self, path):
        match = SCREEN_PATH.fullmatch(path)
        if match:
            for desktop in self.all_desktops():
                if match[2] in desktop.tickets:
                    return desktop.resolve(path)
        raise PermissionDenied()


def mount_desktop(app, store, browser):
    manifest = os.environ.get("AGENTFORGE_DESKTOP_MANIFEST")
    registry = os.environ.get("AGENTFORGE_DESKTOP_SLOTS")
    pool = os.environ.get("AGENTFORGE_DESKTOP_POOL")
    if sum(bool(x) for x in (manifest, registry, pool)) > 1:
        raise ValueError("Choose either manual or managed desktops")
    if not any((manifest, registry, pool)):
        return None
    secret = os.environ.get("SCREEN_PROXY_SECRET", "")
    if len(secret) < 32 or not secret.isascii():
        raise ValueError("Configure a dedicated SCREEN_PROXY_SECRET")
    if registry:
        desktops = []
        for row in json.loads(Path(registry).read_text()):
            owner = [row[k] for k in ("tenant", "space", "user")] + [str(UUID(row["bot"]))]
            path = Path(row["manifest"]).resolve()
            if not all(isinstance(x, str) and x for x in owner) or any(
                d.owner == owner or d.manifest == path for d in desktops
            ):
                raise ValueError("Invalid or duplicate desktop slot")
            desktops.append(Desktop(path, store, browser, owner))
    else:
        desktops = [] if pool else [Desktop(manifest, store, browser)]
    directory = DesktopDirectory(desktops, Path(pool) if pool else None, store, browser)

    reader_secret = os.environ.get("AGENTFORGE_DESKTOP_READER_TOKEN", "")
    if reader_secret:
        if len(reader_secret) < 32 or reader_secret == secret:
            raise ValueError("Set a dedicated desktop reader token")

        @app.post("/api/internal/desktop-reader", include_in_schema=False)
        def reader_lease(body: ReaderRequest, request: Request):
            auth = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(auth, ("Bearer " + reader_secret).encode()):
                raise HTTPException(403)
            desktop = directory.reader(body.generation)
            with desktop.lock:
                entry = desktop.assignment()
                desktop.sync(entry)
                if not entry or entry["generation"] != body.generation:
                    raise HTTPException(404)
                if body.release is not None:
                    if body.release != desktop.reader_id:
                        raise HTTPException(409)
                    desktop.reader_id = None
                    return {"ok": True}
                desktop.authorize(
                    Scope(entry["tenant"], entry["space"], entry["user"]),
                    entry["bot"],
                    True,
                )
                if desktop.controller or desktop.reader_id:
                    raise HTTPException(409)
                desktop.reader_id = secrets.token_urlsafe(32)
                desktop.reader_end = time.monotonic() + 45
                return {"lease": desktop.reader_id}

    @app.post("/api/internal/terminal-access", include_in_schema=False)
    def terminal_access(body: TerminalAccess):
        # The opaque terminal ticket is also presented by the relay before each input.
        for slot in directory.all_desktops():
            if body.key in slot.tickets:
                try:
                    entry, grant = slot.verify(body.key)
                    if grant.terminal and entry["generation"] == body.generation:
                        return {"workspaceBot": grant.bot if slot.team else None}
                except (RunNotFound, RunConflict, PermissionDenied, AuthenticationFailed):
                    pass
                break
        raise HTTPException(403)

    @app.post("/api/internal/screen-target", include_in_schema=False)
    def screen_target(body: TargetRequest, request: Request):
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + secret).encode(),
        ):
            raise HTTPException(403)
        try:
            return directory.resolve(body.path)
        except Exception:
            # All failures revoke the proxy/stream. Never expose upstream credentials.
            raise HTTPException(403) from None

    return directory

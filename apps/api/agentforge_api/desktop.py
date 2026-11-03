"""Original Rakazo screen protocol for an explicitly assigned local Docker desktop."""

import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound, Scope
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

SCREEN_PATH = re.compile(
    r"/novnc/session/(view|control)/([A-Za-z0-9_-]{43})/([\w./-]+)(?:\?[^#\s]*)?", re.ASCII
)
ASSET_PATH = re.compile(
    r"(?:core|vendor|app|include)/[A-Za-z0-9_./-]+|embed\.html|clipboard-bridge\.js|mobile-keyboard\.js|websockify"
)


class ReaderRequest(BaseModel):
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    release: str | None = Field(default=None, max_length=64)


class TargetRequest(BaseModel):
    path: str = Field(max_length=4096)


@dataclass
class Ticket:
    cookie: str
    scope: Scope
    generation: str
    policy: str
    lease: str | None
    expires: float


class Desktop:
    def __init__(self, manifest, store, browser):
        self.manifest = Path(manifest)
        self.store = store
        self.browser = browser
        self.lock = threading.RLock()
        self.tickets = {}
        self.controller = None
        self.lease = None
        self.lease_end = 0
        self.generation = None
        self.reader_id = None
        self.reader_end = 0

    def assignment(self):
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

    def sync(self, entry):
        generation = entry["generation"] if entry else None
        if generation != self.generation or self.lease_end <= time.time():
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
            mode="dedicated",
            kind="docker",
            state="running" if entry else "stopped",
            controlHolder="user"
            if controlled and self.controller == cookie
            else ("bot" if self.reader_id else "none"),
            controlBotId=bot if controlled and self.controller == cookie else None,
            takeoverRequested=False,
            screenAvailable=bool(entry),
            screenWidth=1280,
            screenHeight=800,
            homeRevision=None,
            busyBotName=None,
            canUpdate=False,
            terminalAvailable=False,
        )

    def snapshot(self, scope, bot, cookie, snap):
        entry = self.assignment()
        if self.assigned(entry, scope, bot):
            with self.lock:
                self.authorize(scope, bot)
                snap["computer"] = self.status(entry, bot, cookie)
        return snap

    def handle(self, action, scope, bot, cookie):
        with self.lock:
            entry = self.assignment()
            if not self.assigned(entry, scope, bot):
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
                if self.controller and self.controller != cookie:
                    raise RunConflict("Desktop is controlled by another login session")
                self.controller = cookie
                self.lease = self.lease or str(uuid4())
                self.lease_end = time.time() + 300
                return {
                    "leaseId": self.lease,
                    "expiresAt": datetime.fromtimestamp(self.lease_end, UTC).isoformat(),
                }
            if action == "release":
                if self.controller and self.controller != cookie:
                    raise PermissionDenied()
                self.controller = self.lease = None
                return {"ok": True}
            if action == "heartbeat":
                if self.controller == cookie:
                    self.authorize(scope, bot, True)
                    self.lease_end = time.time() + 300
                return {"ok": True}
            if action != "screenUrl":
                raise ValueError("Unsupported desktop action")
            policy = "control" if self.controller == cookie else "view"
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
                    and v.policy == policy
                    and v.lease == lease
                    and v.expires > now + 30
                ),
                None,
            )
            if key is None:
                key = secrets.token_urlsafe(32)
                self.tickets[key] = Ticket(
                    cookie, scope, self.generation, policy, lease, now + 3600
                )
            prefix = f"/novnc/session/{policy}/{key}"
            view_only = "false" if lease else "true"
            return {"url": f"{prefix}/embed.html?view_only={view_only}&path=websockify"}

    def resolve(self, path):
        match = SCREEN_PATH.fullmatch(path)
        if not match:
            raise PermissionDenied()
        policy, key, asset = match.groups()
        if not ASSET_PATH.fullmatch(asset) or any(p in ("", ".", "..") for p in asset.split("/")):
            raise PermissionDenied()
        with self.lock:
            entry = self.assignment()
            self.sync(entry)
            grant = self.tickets.get(key)
            if not grant or grant.policy != policy or grant.expires <= time.time():
                raise PermissionDenied()
            if (
                not self.assigned(entry, grant.scope)
                or self.browser.resolve(grant.cookie)[0] != grant.scope
            ):
                raise PermissionDenied()
            control = policy == "control"
            if control and (
                not self.lease or grant.lease != self.lease or self.controller != grant.cookie
            ):
                raise PermissionDenied()
            self.authorize(grant.scope, entry["bot"], control)
            suffix = "?token=" + entry[policy] if asset == "websockify" else ""
            return dict(
                protocol="http:",
                hostname="127.0.0.1",
                port=entry["port"],
                path="/" + asset + suffix,
                interactive=control,
            )


def mount_desktop(app, store, browser):
    manifest = os.environ.get("AGENTFORGE_DESKTOP_MANIFEST")
    if not manifest:
        return None
    secret = os.environ.get("SCREEN_PROXY_SECRET", "")
    if len(secret) < 32 or not secret.isascii():
        raise ValueError("Configure a dedicated SCREEN_PROXY_SECRET")
    desktop = Desktop(manifest, store, browser)

    reader_secret = os.environ.get("AGENTFORGE_DESKTOP_READER_TOKEN", "")
    if reader_secret:
        if len(reader_secret) < 32 or reader_secret == secret:
            raise ValueError("Set a dedicated desktop reader token")

        @app.post("/api/internal/desktop-reader", include_in_schema=False)
        def reader_lease(body: ReaderRequest, request: Request):
            auth = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(auth, ("Bearer " + reader_secret).encode()):
                raise HTTPException(403)
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
                    Scope(entry["tenant"], entry["space"], entry["user"]), entry["bot"], True
                )
                if desktop.controller or desktop.reader_id:
                    raise HTTPException(409)
                desktop.reader_id = secrets.token_urlsafe(32)
                desktop.reader_end = time.monotonic() + 45
                return {"lease": desktop.reader_id}

    @app.post("/api/internal/screen-target", include_in_schema=False)
    def screen_target(body: TargetRequest, request: Request):
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + secret).encode()
        ):
            raise HTTPException(403)
        try:
            return desktop.resolve(body.path)
        except Exception:
            # All failures revoke the proxy/stream. Never expose upstream credentials.
            raise HTTPException(403) from None

    return desktop

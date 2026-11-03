"""Native Rakazo email/password protocol plus the existing local-token browser bridge."""

import asyncio
import json
import secrets
import time
from collections import OrderedDict
from threading import BoundedSemaphore, Lock

from agentforge_contracts.rakazo import Login
from agentforge_core.identity import (
    AuthenticationFailed,
    AuthenticationUnavailable,
    PermissionDenied,
)
from agentforge_storage.browser_sessions import BrowserSessions
from agentforge_storage.passwords import PasswordLogin
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from agentforge_api.orpc import RpcError, read_input

COOKIE = "agentforge_session"


class PasswordSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    rememberMe: bool = True
    callbackURL: str | None = Field(default=None, max_length=2048)


def auth_error(code, message, status):
    return JSONResponse(
        {"code": code, "message": message},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def mount_browser_auth(app, settings, store, identities, provider):
    browser = BrowserSessions(identities)
    origin = settings.web_origin
    passwords = PasswordLogin(identities)
    # Limit password KDF concurrency and per-peer request rate. Account lockout is
    # durable in the database; this additional process-local gate bounds unknown-user work.
    password_slots = BoundedSemaphore(2)
    peer_lock = Lock()
    peers = OrderedDict()

    def check_origin(request):
        if not origin or request.headers.get("origin") != origin:
            raise PermissionDenied()

    def authenticate(request):
        if request.headers.get("authorization"):
            scheme, _, token = request.headers["authorization"].partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationFailed()
            scope = provider.authenticate(token)
        else:
            if settings.auth_mode != "database":
                raise AuthenticationFailed()
            check_origin(request)
            scope, _ = browser.resolve(request.cookies.get(COOKIE))
        with store.engine.connect() as conn:
            store.authorization.require(conn, scope)
        selected = request.headers.get("x-rakazo-space-id")
        if selected is not None and selected != scope.space_id:
            raise PermissionDenied()
        return scope

    @app.get("/api/auth/agentforge", response_class=HTMLResponse)
    def login_page():
        if not origin or settings.auth_mode != "database":
            raise AuthenticationUnavailable()
        nonce = secrets.token_urlsafe(24)
        # No credentials or user-controlled text is interpolated into this document.
        html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 AgentForge</title>
<body><main><h1>登录 AgentForge</h1><p>输入管理员为你签发的访问令牌。</p>
<form><label>访问令牌
<input id="token" type="password" autocomplete="off" required maxlength="256"></label>
<button type="submit">进入工作台</button></form><p id="error" role="alert"></p></main>
<script nonce="NONCE">document.querySelector('form').addEventListener('submit',async(e)=>{
e.preventDefault();const input=document.querySelector('#token');
const error=document.querySelector('#error');
error.textContent='';try{const response=await fetch('/api/auth/agentforge/sign-in',{
method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({json:{token:input.value}})});
input.value='';if(!response.ok){error.textContent='登录失败，请检查令牌是否有效。';return;}
location.assign('/app');}catch{error.textContent='暂时无法连接，请重试。';}});</script></body></html>""".replace(
            "NONCE", nonce
        )
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; "
                    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
                ),
            },
        )

    @app.post("/api/auth/agentforge/sign-in")
    async def sign_in(request: Request):
        if settings.auth_mode != "database" or not origin:
            raise AuthenticationUnavailable()
        check_origin(request)
        try:
            body = Login.model_validate(await read_input(request))
        except (RpcError, ValidationError):
            # Do not echo the token in validation errors.
            raise HTTPException(400, "Invalid sign-in request") from None
        raw, lifetime = await run_in_threadpool(browser.create, body.token)
        old = request.cookies.get(COOKIE)
        if old:
            await run_in_threadpool(browser.revoke, old)
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            COOKIE,
            raw,
            httponly=True,
            secure=origin.startswith("https://"),
            samesite="strict",
            max_age=lifetime,
            path="/",
        )
        return response

    @app.get("/api/auth/get-session")
    def get_session(request: Request):
        try:
            if settings.auth_mode != "database" or not origin:
                raise AuthenticationFailed()
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise AuthenticationFailed()
            if request.headers.get("origin") and request.headers["origin"] != origin:
                raise AuthenticationFailed()
            data = browser.session_view(request.cookies.get(COOKIE))
        except (AuthenticationFailed, PermissionDenied):
            data = None
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/sign-out")
    def sign_out(request: Request):
        check_origin(request)
        browser.revoke(request.cookies.get(COOKIE))
        response = JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            COOKIE, path="/", secure=origin.startswith("https://"), httponly=True, samesite="strict"
        )
        return response

    @app.post("/api/auth/sign-in/email")
    async def password_sign_in(request: Request):
        if settings.auth_mode != "database" or not origin:
            return auth_error("PASSWORD_AUTH_DISABLED", "Password login is unavailable", 503)
        try:
            check_origin(request)
        except PermissionDenied:
            return auth_error("INVALID_ORIGIN", "Invalid request origin", 403)
        now = time.monotonic()
        peer = request.client.host if request.client else "unknown"
        with peer_lock:
            started, count = peers.pop(peer, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            peers[peer] = (started, count + 1)
            while len(peers) > 1024:
                peers.popitem(last=False)
        if count >= 30 or not password_slots.acquire(blocking=False):
            return auth_error("TOO_MANY_REQUESTS", "Try again later", 429)
        try:
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return auth_error("INVALID_REQUEST", "Invalid sign-in request", 400)
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 8192:
                            return auth_error("INVALID_REQUEST", "Invalid sign-in request", 400)
            except TimeoutError:
                return auth_error("INVALID_REQUEST", "Invalid sign-in request", 400)
            try:
                data = PasswordSignIn.model_validate(json.loads(body))
            except (ValueError, ValidationError):
                return auth_error("INVALID_REQUEST", "Invalid sign-in request", 400)
            try:
                raw, lifetime = await run_in_threadpool(
                    passwords.sign_in, data.email, data.password
                )
            except (AuthenticationFailed, PermissionDenied):
                return auth_error("INVALID_EMAIL_OR_PASSWORD", "Invalid email or password", 401)
            old = request.cookies.get(COOKIE)
            if old:
                await run_in_threadpool(browser.revoke, old)
            view = await run_in_threadpool(browser.session_view, raw)
            response = JSONResponse(
                {"redirect": False, "user": view["user"]}, headers={"Cache-Control": "no-store"}
            )
            response.set_cookie(
                COOKIE,
                raw,
                httponly=True,
                secure=origin.startswith("https://"),
                samesite="strict",
                path="/",
                max_age=lifetime if data.rememberMe else None,
            )
            return response
        finally:
            password_slots.release()

    @app.get("/api/auth/account-security")
    def account_security(request: Request):
        if settings.auth_mode != "database" or not origin:
            raise AuthenticationFailed()
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            request.headers.get("origin") and request.headers["origin"] != origin
        ):
            raise PermissionDenied()
        scope, _ = browser.resolve(request.cookies.get(COOKIE))
        with store.engine.connect() as conn:
            import sqlalchemy as sa
            from agentforge_storage.schema import web_credentials

            has_password = (
                conn.execute(
                    sa.select(web_credentials.c.credential_id).where(
                        web_credentials.c.tenant_id == scope.tenant_id,
                        web_credentials.c.user_id == scope.user_id,
                    )
                ).first()
                is not None
            )
        return JSONResponse(
            {
                "hasPassword": has_password,
                "passwordChangeEnabled": True,
                "freshOidcAuth": False,
                "ssoLinked": False,
                "emailDeletion": False,
                "sso": None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/sign-up/email")
    def sign_up(request: Request):
        check_origin(request)
        return auth_error(
            "REGISTRATION_CLOSED", "Registration is closed; contact an administrator", 403
        )

    @app.post("/api/auth/{operation:path}")
    def unsupported_auth(operation: str, request: Request):
        check_origin(request)
        # Native controls keep their own error UI; never claim an unsupported action succeeded.
        return auth_error("NOT_IMPLEMENTED", "This account operation is not implemented", 501)

    @app.get("/api/auth/capabilities")
    def capabilities():
        return {
            "sso": None,
            "passwordAuth": bool(origin and settings.auth_mode == "database"),
            "passwordReset": False,
            "resetUrl": None,
            "billing": False,
        }

    @app.get("/api/agentforge/capabilities")
    def platform_capabilities():
        return {
            "rpcProtocol": "orpc-v1-json",
            "rakazoRevision": "d165de43",
            "chat": True,
            "browserLogin": bool(origin and settings.auth_mode == "database"),
            "computer": False,
            "attachments": False,
            "memory": False,
            "approvals": False,
            "groups": False,
            "modelSettings": False,
            "taskEntity": False,
        }

    return browser, authenticate

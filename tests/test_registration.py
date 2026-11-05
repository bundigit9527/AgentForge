from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_core.models import RunConflict
from agentforge_core.settings import Settings
from agentforge_runtime.config import ModelSettings
from agentforge_storage.browser_sessions import BrowserSessions
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.passwords import PasswordLogin
from agentforge_storage.schema import (
    browser_sessions,
    identity_memberships,
    identity_spaces,
    identity_tenants,
    identity_tokens,
    identity_users,
    web_credentials,
)
from fastapi.testclient import TestClient

from tests.test_browser_auth import ORIGIN

SECRET = "signup-test-password"
TABLES = (
    identity_tenants,
    identity_spaces,
    identity_users,
    identity_memberships,
    web_credentials,
    identity_tokens,
    browser_sessions,
)


@pytest.fixture
def registration(settings, repository):
    with TestClient(
        create_app(
            replace(settings, auth_mode="database", web_origin=ORIGIN, registration_enabled=True),
            repository,
            model_settings=ModelSettings(),
        )
    ) as client:
        yield client


def signup(client, **changes):
    return client.post(
        "/api/auth/sign-up/email",
        headers={"Origin": ORIGIN},
        json={
            "email": "new@example.test",
            "password": SECRET,
            "name": "新用户",
            **changes,
        },
    )


def counts(engine):
    with engine.connect() as conn:
        return [conn.scalar(sa.select(sa.func.count()).select_from(t)) for t in TABLES]


def test_registration_cookie_onboarding_and_isolated_workspace(registration, engine):
    client = registration
    response = signup(client, email=" NEW@EXAMPLE.TEST ")
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "new@example.test"
    # Original Web treats token:null as a request to verify email; omit the field.
    assert "token" not in response.json() and SECRET not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    browser = BrowserSessions(IdentityRepository(engine))
    first, _ = browser.resolve(client.cookies["agentforge_session"])
    view = client.get("/api/auth/get-session").json()
    assert view["user"]["name"] == "新用户" and not view["user"]["emailVerified"]

    def rpc(route, body):
        return client.post("/rpc/" + route, headers={"Origin": ORIGIN}, json={"json": body})

    bootstrap = rpc("bootstrap", {}).json()["json"]
    assert bootstrap["bots"] == [] and bootstrap["me"]["spaceId"] == first.space_id
    assert bootstrap["me"]["isDeploymentOwner"] is False
    bot = rpc("bots/create", {"name": "Chief", "spawnKey": "onboarding:first"})
    assert bot.status_code == 200, bot.text
    assert len(rpc("bots/list", {}).json()["json"]) == 1
    previous_cookie = client.cookies["agentforge_session"]
    assert signup(client, email="second@example.test", name="Second").status_code == 200
    second, _ = browser.resolve(client.cookies["agentforge_session"])
    assert first.tenant_id != second.tenant_id and first.space_id != second.space_id
    assert rpc("bots/list", {}).json()["json"] == []
    from agentforge_core.identity import AuthenticationFailed

    with pytest.raises(AuthenticationFailed):
        browser.resolve(previous_cookie)
    client.post("/api/auth/sign-out", headers={"Origin": ORIGIN}, json={})
    login = client.post(
        "/api/auth/sign-in/email",
        headers={"Origin": ORIGIN},
        json={"email": "NEW@example.test", "password": SECRET},
    )
    assert login.status_code == 200
    assert len(rpc("bots/list", {}).json()["json"]) == 1


def test_duplicate_signup_rolls_back_and_keeps_existing_session(registration, engine):
    assert signup(registration).status_code == 200
    before = counts(engine)
    cookie = registration.cookies["agentforge_session"]
    response = signup(registration, email="NEW@EXAMPLE.TEST", password=SECRET + "changed")
    assert response.status_code == 422 and response.json()["code"] == "USER_ALREADY_EXISTS"
    assert counts(engine) == before
    assert registration.cookies["agentforge_session"] == cookie
    assert registration.get("/api/auth/get-session").json() is not None
    PasswordLogin(IdentityRepository(engine)).sign_in("new@example.test", SECRET)


@pytest.mark.parametrize(
    "changes",
    [
        {"password": "short"},
        {"password": "🦊" * 65},
        {"email": "invalid"},
        {"name": "   "},
        {"role": "admin"},
        {"tenant_id": "existing"},
        {"space_id": "existing"},
        {"emailVerified": True},
    ],
)
def test_registration_rejects_invalid_input_and_identity_injection(registration, engine, changes):
    response = signup(registration, **changes)
    assert response.status_code == 400 and SECRET not in response.text
    assert counts(engine) == [0] * len(TABLES)


def test_registration_origin_size_and_rate_limits(registration, engine):
    assert (
        registration.post(
            "/api/auth/sign-up/email", headers={"Origin": "https://other.test"}, json={}
        ).status_code
        == 403
    )
    assert (
        registration.post(
            "/api/auth/sign-up/email",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            content=" " * 8193,
        ).status_code
        == 400
    )
    for _ in range(31):
        response = registration.post("/api/auth/sign-up/email", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 429
    assert counts(engine) == [0] * len(TABLES)


def test_registration_requires_database_auth(settings, repository):
    with TestClient(
        create_app(
            replace(settings, web_origin=ORIGIN, registration_enabled=True),
            repository,
            model_settings=ModelSettings(),
        )
    ) as client:
        assert signup(client).status_code == 503


def test_registration_environment_flag(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", "sqlite://")
    monkeypatch.delenv("AGENTFORGE_REGISTRATION_ENABLED", raising=False)
    assert Settings.from_env().registration_enabled is False
    monkeypatch.setenv("AGENTFORGE_REGISTRATION_ENABLED", "true")
    assert Settings.from_env().registration_enabled is True
    monkeypatch.setenv("AGENTFORGE_REGISTRATION_ENABLED", "typo")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_concurrent_same_email_registration_has_one_account(engine, monkeypatch):
    import agentforge_storage.passwords as module

    barrier = Barrier(2)
    original = module.password_hash

    def synchronize(password, salt=None):
        encoded = original(password, salt)
        barrier.wait(timeout=20)
        return encoded

    monkeypatch.setattr(module, "password_hash", synchronize)
    passwords = PasswordLogin(IdentityRepository(engine))

    def register(email):
        try:
            return passwords.register(email, SECRET, "Concurrent")
        except RunConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(register, ["race@example.test", "RACE@example.test"]))
    assert sum(r is not None for r in result) == 1
    assert counts(engine) == [1] * len(TABLES)

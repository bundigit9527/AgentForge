from dataclasses import replace

import pytest
import sqlalchemy as sa
from agentforge_core.identity import AuthenticationFailed, PermissionDenied
from agentforge_core.models import RunConflict
from agentforge_storage.passwords import PasswordLogin
from agentforge_storage.schema import browser_sessions, web_credentials

from tests.test_browser_auth import ORIGIN
from tests.test_browser_auth import browser_client as _browser_client
from tests.test_identity import people as _people

browser_client = _browser_client
people = _people
SECRET = "test-native-password-only"


def login(client, email="alice@example.test", password=SECRET, **extra):
    return client.post(
        "/api/auth/sign-in/email",
        headers={"Origin": ORIGIN},
        json={"email": email, "password": password, **extra},
    )


@pytest.fixture
def native(people, browser_client):
    passwords = PasswordLogin(people[0])
    passwords.set_password(people[1]["alice"], "alice@example.test", SECRET)
    return browser_client, passwords


def test_native_login_cookie_contract_and_salted_password_storage(native, engine):
    client, _ = native
    caps = client.get("/api/auth/capabilities").json()
    assert caps["passwordAuth"] is True and "agentforgeTokenSignIn" not in caps
    result = login(client)
    assert result.status_code == 200, result.text
    assert result.json()["user"]["email"] == "alice@example.test"
    assert "token" not in result.json() and SECRET not in result.text
    assert (
        "HttpOnly" in result.headers["set-cookie"]
        and "Max-Age=28800" in result.headers["set-cookie"]
    )
    assert client.get("/api/auth/get-session").json()["user"]["id"] == "alice"
    with engine.connect() as conn:
        row = conn.execute(sa.select(web_credentials)).mappings().one()
        assert row["password_hash"].startswith("scrypt$131072$8$1$")
        assert SECRET not in str(row)
        session = conn.execute(sa.select(browser_sessions)).mappings().one()
        assert client.cookies["agentforge_session"] not in str(session)
    old = client.cookies["agentforge_session"]
    result = login(client, rememberMe=False)
    assert result.status_code == 200 and "Max-Age" not in result.headers["set-cookie"]
    client.cookies.clear()
    client.cookies.set("agentforge_session", old)
    assert client.get("/api/auth/get-session").json() is None


def test_login_failures_do_not_leak_secrets_or_account_existence(native):
    client, _ = native
    wrong = login(client, password="wrong").json()
    unknown = login(client, email="unknown@example.test").json()
    assert (
        wrong
        == unknown
        == {"code": "INVALID_EMAIL_OR_PASSWORD", "message": "Invalid email or password"}
    )
    cross = client.post(
        "/api/auth/sign-in/email",
        headers={"Origin": "http://evil.example"},
        json={"email": "alice@example.test", "password": SECRET},
    )
    assert cross.status_code == 403
    for body in [
        {"email": "alice@example.test", "password": SECRET, "tenant": "override"},
        {"email": "alice@example.test", "password": SECRET * 1024},
    ]:
        result = client.post("/api/auth/sign-in/email", headers={"Origin": ORIGIN}, json=body)
        assert result.status_code == 400 and SECRET not in result.text


def test_durable_account_lockout_and_peer_rate_limit(native, clock):
    client, _ = native
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 401
    clock.advance(901)
    assert login(client).status_code == 200
    for _ in range(30):
        result = client.post("/api/auth/sign-in/email", headers={"Origin": ORIGIN}, json={})
    assert result.status_code == 429


def test_password_reset_and_member_revocation_invalidate_sessions(native, people):
    client, passwords = native
    assert login(client).status_code == 200
    old = client.cookies["agentforge_session"]
    passwords.set_password(people[1]["alice"], "alice@example.test", SECRET + "-new")
    assert client.get("/api/auth/get-session").json() is None
    assert login(client).status_code == 401
    assert people[0].authenticate(people[2]["alice"]["token"]) == people[1]["alice"]
    assert login(client, password=SECRET + "-new").status_code == 200
    assert client.cookies["agentforge_session"] != old
    people[0].set_member(
        people[1]["admin"], "alice", display_name="Alice", role="member", active=False
    )
    assert client.get("/api/auth/get-session").json() is None
    assert login(client, password=SECRET + "-new").status_code == 401


def test_provisioning_requires_existing_member_and_global_email_is_unique(native, people):
    _, passwords = native
    with pytest.raises(RunConflict):
        passwords.set_password(people[1]["bob"], "ALICE@EXAMPLE.TEST", SECRET)
    with pytest.raises(ValueError):
        passwords.set_password(people[1]["bob"], "bob@example.test", "short")
    with pytest.raises(PermissionDenied):
        passwords.set_password(
            replace(people[1]["bob"], user_id="missing"), "missing@example.test", SECRET
        )
    assert passwords.sign_in("Alice@Example.Test", SECRET)[0].startswith("afs_")


def test_password_reset_wins_over_stale_login_verification(native, people, monkeypatch):
    _, passwords = native
    import agentforge_storage.passwords as module

    verify = module.matches

    def reset_before_check(password, encoded):
        passwords.set_password(people[1]["alice"], "alice@example.test", SECRET + "-new")
        return verify(password, encoded)

    monkeypatch.setattr(module, "matches", reset_before_check)
    with pytest.raises(AuthenticationFailed):
        passwords.sign_in("alice@example.test", SECRET)


def test_registration_and_unsupported_account_operations_return_native_errors(native):
    client, _ = native
    response = client.post("/api/auth/sign-up/email", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 403 and response.json()["code"] == "REGISTRATION_CLOSED"
    assert login(client).status_code == 200
    assert client.get("/api/auth/account-security").json()["hasPassword"] is True
    response = client.post("/api/auth/delete-user", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 501 and response.json()["code"] == "NOT_IMPLEMENTED"


def test_operator_password_command_does_not_print_password(
    native, people, monkeypatch, capsys, database_url
):
    from agentforge_api import admin

    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", database_url)
    scope = people[1]["bob"]
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentforge-admin",
            "set-password",
            "--tenant",
            scope.tenant_id,
            "--space",
            scope.space_id,
            "--user",
            scope.user_id,
            "--email",
            "bob@example.test",
        ],
    )
    monkeypatch.setattr(admin, "getpass", lambda _: SECRET)
    admin.main()
    output = capsys.readouterr().out
    assert "configured" in output and SECRET not in output
    assert native[1].sign_in("bob@example.test", SECRET)


def test_password_migration_preserves_token_sessions_and_invalidates_password_sessions(
    native, people, engine
):
    from pathlib import Path

    from agentforge_storage.browser_sessions import BrowserSessions
    from alembic import command
    from alembic.config import Config

    client, _ = native
    assert login(client).status_code == 200
    passwords_cookie = client.cookies["agentforge_session"]
    browser = BrowserSessions(people[0])
    token_cookie, _ = browser.create(people[2]["alice"]["token"])
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0008")
        command.upgrade(config, "head")
    assert browser.resolve(token_cookie)[0] == people[1]["alice"]
    with pytest.raises(AuthenticationFailed):
        browser.resolve(passwords_cookie)


def test_account_security_checks_origin_and_disabled_auth_mode(native, settings, repository):
    from agentforge_api.app import create_app
    from agentforge_runtime.config import ModelSettings
    from fastapi.testclient import TestClient

    client, _ = native
    assert login(client).status_code == 200
    assert (
        client.get(
            "/api/auth/account-security", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    with TestClient(
        create_app(
            replace(settings, web_origin=ORIGIN, auth_mode="development"),
            repository,
            model_settings=ModelSettings(),
        )
    ) as disabled:
        disabled.cookies.set("agentforge_session", client.cookies["agentforge_session"])
        assert disabled.get("/api/auth/account-security").status_code == 401


@pytest.mark.parametrize("revoke", [False, True])
def test_change_password_rotates_requested_sessions(native, people, revoke):
    client, passwords = native
    other, _ = passwords.sign_in("alice@example.test", SECRET)
    token = people[2]["alice"]["token"]
    bridge, _ = passwords.browser.create(token)
    assert login(client).is_success
    old = client.cookies["agentforge_session"]
    new = "abcdefgh"

    def change(current, origin=ORIGIN):
        return client.post(
            "/api/auth/change-password",
            headers={"Origin": origin},
            json={"currentPassword": current, "newPassword": new, "revokeOtherSessions": revoke},
        )

    for invalid, code in [("short", "PASSWORD_TOO_SHORT"), ("🦊" * 65, "PASSWORD_TOO_LONG")]:
        response = client.post(
            "/api/auth/change-password",
            headers={"Origin": ORIGIN},
            json={"currentPassword": SECRET, "newPassword": invalid},
        )
        assert response.status_code == 400 and response.json()["code"] == code
    assert change(SECRET, "https://other.example").status_code == 403
    assert change("wrong").json()["code"] == "INVALID_PASSWORD"
    passwords.browser.resolve(other)
    passwords.browser.resolve(old)
    result = change(SECRET)
    assert result.is_success and result.json()["user"]["email"] == "alice@example.test"
    current = client.cookies["agentforge_session"]
    assert (current != old) == revoke
    assert current not in result.text and new not in result.text
    assert client.get("/api/auth/get-session").json()["user"]["id"] == "alice"
    for session in (other, bridge, old):
        if revoke:
            with pytest.raises(AuthenticationFailed):
                passwords.browser.resolve(session)
        else:
            passwords.browser.resolve(session)
    assert people[0].authenticate(token) == people[1]["alice"]
    assert login(client).status_code == 401
    assert login(client, password=new).is_success


def test_password_change_loses_to_operator_reset(native, people, monkeypatch):
    import agentforge_storage.passwords as module

    client, passwords = native
    assert login(client).is_success
    verify = module.matches

    def concurrent_reset(password, encoded):
        passwords.set_password(people[1]["alice"], "alice@example.test", SECRET + "-admin")
        return verify(password, encoded)

    monkeypatch.setattr(module, "matches", concurrent_reset)
    result = client.post(
        "/api/auth/change-password",
        headers={"Origin": ORIGIN},
        json={
            "currentPassword": SECRET,
            "newPassword": SECRET + "-loser",
            "revokeOtherSessions": True,
        },
    )
    assert result.status_code == 400
    monkeypatch.setattr(module, "matches", verify)
    assert login(client, password=SECRET + "-loser").status_code == 401
    assert login(client, password=SECRET + "-admin").is_success

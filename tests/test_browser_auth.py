from dataclasses import replace

import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_runtime.config import ModelSettings
from agentforge_storage.schema import browser_sessions
from fastapi.testclient import TestClient

from tests.test_identity import people as _people

people = _people

ORIGIN = "http://testserver"


@pytest.fixture
def browser_client(settings, repository, people):
    with TestClient(
        create_app(
            replace(settings, auth_mode="database", web_origin=ORIGIN),
            repository,
            model_settings=ModelSettings(),
        )
    ) as client:
        yield client


def login(client, token, origin=ORIGIN):
    return client.post(
        "/api/auth/agentforge/sign-in", headers={"Origin": origin}, json={"json": {"token": token}}
    )


def test_cookie_login_rpc_and_better_auth_session_without_exposing_credentials(
    browser_client, people, engine
):
    client = browser_client
    token = people[2]["alice"]["token"]
    assert client.get("/api/auth/get-session").json() is None
    assert client.get("/api/auth/agentforge").status_code == 200
    result = login(client, token)
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=28800" in cookie
    session = client.get("/api/auth/get-session")
    assert session.json()["user"]["id"] == "alice"
    assert token not in session.text and '"token"' not in session.text
    assert session.headers["cache-control"] == "no-store"
    result = client.post("/rpc/me", headers={"Origin": ORIGIN}, json={})
    assert result.status_code == 200 and result.json()["json"]["userId"] == "alice"
    with engine.connect() as conn:
        row = str(conn.execute(sa.select(browser_sessions)).mappings().one())
        assert token not in row and client.cookies["agentforge_session"] not in row
    assert client.post("/api/auth/sign-out", headers={"Origin": ORIGIN}, json={}).status_code == 200
    assert client.get("/api/auth/get-session").json() is None
    # Signing out a browser does not revoke the operator-issued access token.
    assert people[0].authenticate(token) == people[1]["alice"]


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", "http://testserver.evil.example"]
)
def test_cookie_operations_reject_cross_origin(browser_client, people, origin):
    assert login(browser_client, people[2]["alice"]["token"], origin).status_code == 403
    login(browser_client, people[2]["alice"]["token"])
    for path in ["/rpc/bots/create", "/api/auth/sign-out"]:
        assert (
            browser_client.post(
                path, headers={"Origin": origin}, json={"json": {"name": "bad"}}
            ).status_code
            == 403
        )
    assert browser_client.post("/rpc/me", json={}).status_code == 403


@pytest.mark.parametrize("revocation", ["expiry", "token", "member", "session"])
def test_browser_session_obeys_revocations(browser_client, people, clock, revocation):
    identities, users, tokens = people
    token = (
        identities.issue_token(users["alice"], lifetime_seconds=60)
        if revocation == "expiry"
        else tokens["alice"]
    )
    login(browser_client, token["token"])
    if revocation == "expiry":
        clock.advance(61)
    elif revocation == "token":
        identities.revoke_token(users["alice"], token["token_id"])
    elif revocation == "member":
        identities.set_member(
            users["admin"], "alice", display_name="Alice", role="member", active=False
        )
    else:
        browser_client.post("/api/auth/sign-out", headers={"Origin": ORIGIN}, json={})
    assert browser_client.get("/api/auth/get-session").json() is None
    assert browser_client.post("/rpc/me", headers={"Origin": ORIGIN}, json={}).status_code in {
        401,
        403,
    }


def test_invalid_bearer_does_not_fall_back_to_valid_cookie(browser_client, people):
    login(browser_client, people[2]["alice"]["token"])
    result = browser_client.post(
        "/rpc/me", headers={"Origin": ORIGIN, "Authorization": "Bearer bad"}, json={}
    )
    assert result.status_code == 401


def test_cookie_auth_is_opt_in_and_development_mode_has_no_browser_login(client, headers):
    assert client.get("/api/auth/agentforge").status_code == 503
    assert client.get("/api/auth/get-session").json() is None
    assert client.get("/api/auth/capabilities").json()["passwordAuth"] is False
    assert client.get("/api/agentforge/capabilities").json()["browserLogin"] is False


def test_login_validation_does_not_echo_token(browser_client):
    secret = "private-test-value"
    response = login(browser_client, secret)
    assert response.status_code == 400 and secret not in response.text


def test_relogin_rotates_and_revokes_previous_browser_session(browser_client, people):
    login(browser_client, people[2]["alice"]["token"])
    old = browser_client.cookies["agentforge_session"]
    login(browser_client, people[2]["bob"]["token"])
    assert browser_client.cookies["agentforge_session"] != old
    assert browser_client.get("/api/auth/get-session").json()["user"]["id"] == "bob"
    browser_client.cookies.clear()
    browser_client.cookies.set("agentforge_session", old)
    assert browser_client.get("/api/auth/get-session").json() is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/path",
        "https://example.com/",
        "file:///tmp",
        "https://u:p@example.com",
        "https://example.com?token=x",
    ],
)
def test_invalid_browser_origin_is_rejected(settings, origin):
    with pytest.raises(ValueError):
        replace(settings, web_origin=origin)

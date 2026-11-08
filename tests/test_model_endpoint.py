import socket

import httpx
import pytest
from agentforge_connectors import model_endpoint as endpoint
from agentforge_core.model_management import ModelManagementError


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://u:p@example.com",
        "https://example.com?key=x",
        "https://example.com#fragment",
        "file:///etc/passwd",
        "https://example.com\\@127.0.0.1",
        "https://example.com:999999",
    ],
)
def test_reject_unsafe_endpoint(url):
    with pytest.raises(ModelManagementError):
        endpoint.endpoint(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "0.0.0.0"]
)
def test_private_dns_never_receives_credential(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, (address, 443))]
    )
    transport = endpoint.ModelEndpointTransport("https://models.example/v1")
    seen = []
    transport.inner.close()
    transport.inner = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ModelManagementError):
            client.get("https://models.example/v1/models", headers={"Authorization": "secret"})
    assert not seen


def test_transport_pins_dns_and_keeps_tls_hostname(monkeypatch):
    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, (next(answers), 443))]
    )
    transport = endpoint.ModelEndpointTransport("https://models.example/v1")
    seen = []
    transport.inner.close()
    transport.inner = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        client.get("https://models.example/v1/models")
        with pytest.raises(ModelManagementError):
            client.get("https://models.example/v1/models")
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "models.example"
    assert seen[0].extensions["sni_hostname"] == "models.example"


def test_operator_can_explicitly_allow_internal_origin(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_MODEL_ALLOWED_ORIGINS", '["http://127.0.0.1:8900"]')
    transport = endpoint.ModelEndpointTransport("http://127.0.0.1:8900/v1")
    transport.inner.close()
    transport.inner = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": [{"id": "local"}]})
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("http://127.0.0.1:8900/v1/models").status_code == 200
        with pytest.raises(ModelManagementError):
            client.get("http://127.0.0.1:8901/v1/models")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
        httpx.Response(200, content=b"x" * 1048577),
        httpx.Response(200, json={"data": []}),
        httpx.Response(401, text="secret"),
    ],
)
def test_discovery_rejects_redirects_oversize_and_bad_data(monkeypatch, response):
    seen = []
    monkeypatch.setattr(
        endpoint,
        "ModelEndpointTransport",
        lambda _: httpx.MockTransport(lambda r: seen.append(r) or response),
    )
    with pytest.raises(ModelManagementError) as exc:
        endpoint.discover("https://models.example/v1", "test-key")
    assert "secret" not in str(exc.value) and len(seen) == 1


@pytest.mark.parametrize("padded", [True, False])
def test_generated_encryption_keys_support_documented_base64(monkeypatch, padded):
    import base64
    import secrets

    from agentforge_connectors.model_secrets import seal, unseal

    encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(
        "AGENTFORGE_MODEL_ENCRYPTION_KEY", encoded if padded else encoded.rstrip("=")
    )
    row = dict(
        tenant_id="t",
        user_id="u",
        connection_id="c",
        revision=1,
        provider="deepseek",
        configuration={"baseUrl": "https://api.deepseek.com"},
    )
    row["encrypted_key"] = seal(row, "test-only-secret")
    assert unseal(row) == "test-only-secret"
    with pytest.raises(ModelManagementError):
        unseal({**row, "configuration": {"baseUrl": "https://another.example"}})

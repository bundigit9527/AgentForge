import pytest
from agentforge_runtime.config import ModelSettings

from tests.model_support import model_settings


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "typo"},
        {"base_url": ""},
        {"base_url": "file:///tmp/model"},
        {"base_url": "https://user:password@gateway.example/v1"},
        {"base_url": "https://gateway.example/v1?key=private"},
        {"base_url": "https://gateway.example:bad/v1"},
        {"api_key": ""},
        {"api_key": "token\ninjection"},
        {"model": " "},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
        {"max_rounds": 0},
        {"max_rounds": 33},
        {"max_completion_tokens": 0},
    ],
)
def test_invalid_model_configuration_fails_before_network(changes):
    with pytest.raises(ValueError):
        model_settings(**changes)


def test_credentials_are_not_printed_and_numeric_errors_hide_values(monkeypatch):
    settings = model_settings(api_key="private-api-key")
    assert "private-api-key" not in repr(settings)
    monkeypatch.setenv("AGENTFORGE_MODEL_MAX_ROUNDS", "private-invalid-value")
    with pytest.raises(ValueError) as error:
        ModelSettings.from_env()
    assert "private-invalid-value" not in str(error.value)

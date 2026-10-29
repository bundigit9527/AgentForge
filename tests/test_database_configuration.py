import pytest
from sqlalchemy.engine import make_url

from tests.postgresql_support import PostgreSQLTestDatabase, validate_postgresql_test_url


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-a-url",
        "sqlite:///agentforge_test",
        "postgresql://user@localhost/agentforge_test",
        "postgresql+psycopg://user@localhost/agentforge_dev",
        "postgresql+psycopg://user@/agentforge_test",
        "postgresql+psycopg://localhost/agentforge_test",
        "postgresql+psycopg://user@localhost/agentforge_test?options=-csearch_path=public",
        "postgresql+psycopg://user@localhost/agentforge_test?dbname=production",
        "postgresql+psycopg://user@localhost/agentforge_test?service=production",
        "postgresql+psycopg://user@localhost/agentforge_test?host=elsewhere",
        "postgresql+psycopg://user@localhost/agentforge_test?connect_timeout=0",
        "postgresql+psycopg://user@localhost/agentforge_test?connect_timeout=16",
        "postgresql+psycopg://user@localhost/agentforge_test?connect_timeout=5&connect_timeout=6",
    ],
)
def test_invalid_test_database_is_rejected_without_connecting(value):
    with pytest.raises(ValueError):
        validate_postgresql_test_url(value)


def test_valid_url_retains_tls_and_encoded_credentials():
    value = "postgresql+psycopg://tester:p%40ss@localhost:55433/agentforge_test?sslmode=require"
    url = make_url(validate_postgresql_test_url(value))
    assert url.password == "p@ss"
    assert url.port == 55433
    assert url.query == {"sslmode": "require", "connect_timeout": "5"}


def test_validation_errors_and_fixture_repr_do_not_disclose_credentials():
    value = "postgresql+psycopg://user:private-password@localhost/production"
    with pytest.raises(ValueError) as error:
        validate_postgresql_test_url(value)
    assert "private-password" not in str(error.value)
    assert "private-password" not in repr(PostgreSQLTestDatabase(value, "af_test_example"))

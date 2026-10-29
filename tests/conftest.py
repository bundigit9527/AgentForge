import os
from pathlib import Path

import pytest
from agentforge_api.app import create_app
from agentforge_core.models import Scope
from agentforge_core.settings import Settings
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests.postgresql_support import isolated_postgresql_database, validate_postgresql_test_url

ROOT = Path(__file__).resolve().parents[1]
TEST_TOKEN = "test-token-for-offline-checks-only-123456"
POSTGRESQL_URL = pytest.StashKey[str]()


def pytest_addoption(parser):
    parser.addoption(
        "--database-backend",
        choices=("sqlite", "postgresql"),
        default="sqlite",
        help="Default: offline SQLite. PostgreSQL requires AGENTFORGE_TEST_DATABASE_URL.",
    )


def pytest_configure(config):
    if config.getoption("--database-backend") == "postgresql":
        try:
            config.stash[POSTGRESQL_URL] = validate_postgresql_test_url(
                os.environ.get("AGENTFORGE_TEST_DATABASE_URL")
            )
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from None


def pytest_report_header(config):
    return f"AgentForge test database: {config.getoption('--database-backend')}"


def pytest_collection_modifyitems(config, items):
    if config.getoption("--database-backend") == "sqlite":
        skip = pytest.mark.skip(reason="Requires --database-backend=postgresql and a test database")
        for item in items:
            if "postgresql" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def postgresql_url(pytestconfig):
    if pytestconfig.getoption("--database-backend") != "postgresql":
        pytest.skip("PostgreSQL integration mode is not enabled")
    return pytestconfig.stash[POSTGRESQL_URL]


class Clock:
    value = 1800000000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def database_url(tmp_path, pytestconfig, request):
    if pytestconfig.getoption("--database-backend") == "sqlite":
        yield f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
    else:
        with isolated_postgresql_database(request.getfixturevalue("postgresql_url")) as database:
            yield database.url


@pytest.fixture
def engine(database_url):
    engine = create_database(database_url)
    config = Config(str(ROOT / "alembic.ini"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repository(engine, clock):
    return RunRepository(engine, clock)


@pytest.fixture
def scope():
    return Scope("tenant-a", "space-a", "user-a")


@pytest.fixture
def settings(database_url, scope):
    return Settings(database_url=database_url, dev_token=TEST_TOKEN, dev_scope=scope)


@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(settings, repository):
    with TestClient(create_app(settings, repository)) as client:
        yield client

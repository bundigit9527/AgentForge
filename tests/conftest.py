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

ROOT = Path(__file__).resolve().parents[1]
TEST_TOKEN = "test-token-for-offline-checks-only-123456"


class Clock:
    value = 1800000000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def database_url(tmp_path):
    return f"sqlite+pysqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(database_url):
    engine = create_database(database_url)
    config = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield engine
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

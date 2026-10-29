"""Opt-in PostgreSQL fixtures; never reuse the application's database URL."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from agentforge_storage.database import create_database
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, DropSchema

ALLOWED_QUERY_KEYS = {"connect_timeout", "sslmode", "sslrootcert", "sslcert", "sslkey"}


def validate_postgresql_test_url(value: str | None) -> str:
    if not value:
        raise ValueError("PostgreSQL tests require AGENTFORGE_TEST_DATABASE_URL.")
    try:
        url = make_url(value)
        valid = (
            url.drivername == "postgresql+psycopg"
            and bool(url.host)
            and bool(url.username)
            and bool(url.database)
            and url.database.endswith("_test")
            and set(url.query) <= ALLOWED_QUERY_KEYS
        )
        timeout = url.query.get("connect_timeout", "5")
        valid = valid and isinstance(timeout, str) and timeout.isdigit() and 1 <= int(timeout) <= 15
    except (SQLAlchemyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(
            "Use postgresql+psycopg with an explicit host and username, a dedicated database "
            "ending in _test, and connect_timeout between 1 and 15 seconds. "
            "Only connect_timeout and TLS query parameters are accepted."
        )
    return url.update_query_dict({"connect_timeout": timeout}).render_as_string(hide_password=False)


@dataclass(frozen=True)
class PostgreSQLTestDatabase:
    url: str = field(repr=False)
    schema: str


@contextmanager
def isolated_postgresql_database(value: str) -> Iterator[PostgreSQLTestDatabase]:
    url = make_url(validate_postgresql_test_url(value))
    schema = f"af_test_{uuid4().hex}"
    # libpq options apply to every connection, including new Worker/API engines.
    limits = "-cstatement_timeout=15000 -clock_timeout=5000"
    admin_url = url.update_query_dict({"options": f"-csearch_path=pg_catalog {limits}"})
    engine = create_database(admin_url.render_as_string(hide_password=False))
    created = False
    try:
        try:
            with engine.begin() as connection:
                connection.execute(CreateSchema(schema))
            created = True
        except SQLAlchemyError:
            raise RuntimeError(
                "Cannot create an isolated PostgreSQL test schema. Check the test service, "
                "credentials, and CREATE permission on the dedicated test database."
            ) from None
        scoped_url = url.update_query_dict({"options": f"-csearch_path={schema} {limits}"})
        yield PostgreSQLTestDatabase(scoped_url.render_as_string(hide_password=False), schema)
    finally:
        try:
            if created:
                try:
                    with engine.begin() as connection:
                        connection.execute(DropSchema(schema, cascade=True))
                except SQLAlchemyError:
                    raise RuntimeError(
                        f"Could not remove test schema {schema}; check the dedicated test database."
                    ) from None
        finally:
            engine.dispose()

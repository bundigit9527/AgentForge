import os

from agentforge_storage.database import create_database
from agentforge_storage.schema import metadata
from alembic import context

config = context.config


def database_url() -> str:
    url = os.environ.get("AGENTFORGE_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise ValueError("AGENTFORGE_DATABASE_URL is required for migrations")
    return url


def migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    engine = create_database(database_url())
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()

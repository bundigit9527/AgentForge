from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url


def create_database(url: str) -> Engine:
    backend = make_url(url).get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError("Only SQLite (offline development) and PostgreSQL are supported")
    kwargs = {"check_same_thread": False, "timeout": 10} if backend == "sqlite" else {}
    engine = create_engine(url, connect_args=kwargs, pool_pre_ping=True)
    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine

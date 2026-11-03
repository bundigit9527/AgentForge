"""Explicit, loopback-only Web E2E fixture; never loads .env or the development database."""

import argparse
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import uvicorn
from agentforge_api.app import create_app
from agentforge_core.models import Scope
from agentforge_core.settings import Settings
from agentforge_runtime.config import ModelSettings
from agentforge_storage.database import create_database
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.passwords import PasswordLogin
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from alembic import command
from alembic.config import Config
from fastapi import HTTPException


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--web-port", type=int, default=15173)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="agentforge-web-e2e-") as directory:
        url = f"sqlite+pysqlite:///{directory}/test.db"
        engine = create_database(url)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        identities = IdentityRepository(engine)
        tokens = {}
        users = {}
        web_logins = {}
        passwords = PasswordLogin(identities)
        store = RunRepository(engine, auth_mode="database")
        app = create_app(
            Settings(
                database_url=url,
                auth_mode="database",
                web_origin=f"http://127.0.0.1:{args.web_port}",
            ),
            store,
            model_settings=ModelSettings(),
        )

        @app.get("/api/__test__/fixture")
        def fixture():
            # Fresh identities keep prior history and revocations out of subsequent suites.
            admin = Scope("web-test-" + uuid4().hex, "web-test", "admin")
            identities.bootstrap(
                admin,
                tenant_name="Test",
                space_name="Web Test",
                display_name="Test Admin",
            )
            tokens.clear()
            users.clear()
            web_logins.clear()
            for name in ("alice", "bob"):
                identities.set_member(admin, name, display_name=name.title(), role="member")
                users[name] = Scope(admin.tenant_id, admin.space_id, name)
                tokens[name] = identities.issue_token(users[name], lifetime_seconds=3600)
                email = name + "+" + admin.tenant_id + "@example.test"
                password = secrets.token_urlsafe(24)
                passwords.set_password(users[name], email, password)
                web_logins[name] = {"email": email, "password": password}
            return {
                "testOnly": True,
                "webLogins": web_logins,
                "tokens": {name: item["token"] for name, item in tokens.items()},
            }

        @app.post("/api/__test__/work")
        def work():
            return {"worked": Worker(store).run_once()}

        @app.post("/api/__test__/revoke/{name}")
        def revoke(name: str):
            if name not in tokens:
                raise HTTPException(404)
            identities.revoke_token(users[name], tokens[name]["token_id"])
            passwords.set_password(
                users[name], web_logins[name]["email"], secrets.token_urlsafe(32)
            )
            return {"ok": True}

        try:
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
            )
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()

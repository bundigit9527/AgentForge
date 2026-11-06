"""Opt-in real-model/original-Web acceptance using disposable PostgreSQL and Computer."""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
from agentforge_core.models import Scope
from agentforge_runtime.config import ModelSettings
from agentforge_storage.database import create_database
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.passwords import PasswordLogin
from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--rakazo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--browser-script", type=Path, default=Path(__file__).with_name("rakazo_p0.mts")
    )
    parser.add_argument("--image", default="rakazo/computer:local")
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--web-port", type=int, default=15173)
    args = parser.parse_args()
    model = ModelSettings.from_env()
    if model.provider != "chat_completions":
        parser.error("Configure a real Chat Completions model explicitly before running")
    # Do not inherit development DB, grants, static desktops or MCP endpoints.
    base = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFORGE_")}
    base.pop("SCREEN_PROXY_SECRET", None)
    base["DOCKER_CONTEXT"] = os.environ.get("DOCKER_CONTEXT", "colima")
    base["PYTHONDONTWRITEBYTECODE"] = "1"
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output, 0o700)
    for port in (args.api_port, args.web_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], env=base, text=True
    ).strip()
    name = "agentforge-p0-" + uuid4().hex[:12]
    processes = []
    with ExitStack() as resources:
        state = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="af-p0-")))
        pool = state / "slots"
        pool.mkdir(mode=0o700)
        password = secrets.token_hex(24)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_USER=agentforge",
                "-e",
                "POSTGRES_DB=agentforge_p0_test",
                "-p",
                "127.0.0.1::5432",
                "postgres:17.11-bookworm",
            ],
            env={**base, "POSTGRES_PASSWORD": password},
            check=True,
            stdout=subprocess.DEVNULL,
        )
        resources.callback(
            lambda: subprocess.run(
                ["docker", "stop", "-t", "10", name],
                env=base,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        port = (
            subprocess.check_output(["docker", "port", name, "5432/tcp"], env=base, text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        database_url = (
            f"postgresql+psycopg://agentforge:{password}@127.0.0.1:{port}/agentforge_p0_test"
        )
        engine = create_database(database_url)
        resources.callback(engine.dispose)
        for _ in range(60):
            try:
                with engine.connect():
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        config = Config(str(args.repo / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        identities = IdentityRepository(engine)
        scope = Scope("p0-" + uuid4().hex, "acceptance", "alice")
        identities.bootstrap(scope, tenant_name="P0", space_name="Acceptance", display_name="Alice")
        identities.set_member(scope, "bob", display_name="Bob", role="member")
        logins = {}
        for user in ("alice", "bob"):
            identity = Scope(scope.tenant_id, scope.space_id, user)
            login = {"email": f"{user}@p0.example.test", "password": secrets.token_urlsafe(24)}
            PasswordLogin(identities).set_password(identity, **login)
            logins[user] = {
                **login,
                "token": identities.issue_token(identity, lifetime_seconds=3600)["token"],
            }
        policy = state / "browser.json"
        policy.write_text(
            json.dumps(
                {
                    "grants": [
                        {
                            "tenant_id": scope.tenant_id,
                            "space_id": scope.space_id,
                            "users": ["alice", "bob"],
                            "origins": ["https://www.python.org"],
                        }
                    ]
                }
            )
        )
        api = f"http://127.0.0.1:{args.api_port}"
        web = f"http://127.0.0.1:{args.web_port}"
        common = {
            **base,
            "AGENTFORGE_AUTH_MODE": "database",
            "AGENTFORGE_DATABASE_URL": database_url,
            "AGENTFORGE_WEB_ORIGIN": web,
            "AGENTFORGE_DESKTOP_POOL": str(pool / "pool.sock"),
            "AGENTFORGE_WORKSPACE_CHECKPOINTS": str(state / "checkpoints"),
            "AGENTFORGE_BROWSER_POLICY_FILE": str(policy),
            "AGENTFORGE_BROWSER_API_ORIGIN": api,
            "AGENTFORGE_TERMINAL_API_ORIGIN": api,
            "AGENTFORGE_BROWSER_TOKEN": secrets.token_hex(32),
            "SCREEN_PROXY_SECRET": secrets.token_hex(32),
            "AGENTFORGE_COMPUTER_IMAGE": image,
            "AGENTFORGE_DESKTOP_LIMIT": "3",
            "AGENTFORGE_DESKTOP_IDLE_SECONDS": "600",
            "AGENTFORGE_ALLOWED_TOOLS": "example_report@1,text_stats@1",
        }
        real = {
            **common,
            **{k: v for k, v in os.environ.items() if k.startswith("AGENTFORGE_MODEL_")},
        }
        real.update(
            AGENTFORGE_MODEL_STREAM="true",
            AGENTFORGE_MODEL_MAX_ROUNDS="16",
            AGENTFORGE_MODEL_TIMEOUT_SECONDS="120",
        )
        fixture = state / "fixture.json"
        fixture.write_text(json.dumps({"logins": logins, "model": model.model, "origin": web}))
        fixture.chmod(0o600)

        def start(label, argv, env, cwd=args.repo):
            log = resources.enter_context((args.output / (label + ".log")).open("w"))
            proc = subprocess.Popen(
                argv, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            processes.append(proc)
            return proc

        def stop_all():
            for proc in reversed(processes):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=100)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()

        resources.callback(stop_all)
        start("supervisor", [sys.executable, "-m", "agentforge_supervisor.desktop_pool"], common)
        start(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentforge_api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
                "--no-access-log",
            ],
            real,
        )
        start("worker", [sys.executable, "-m", "agentforge_worker.main", "--watch"], real)
        start(
            "web",
            ["pnpm", "--filter", "@rakazo/web", "dev"],
            {
                **base,
                "NODE_ENV": "development",
                "API_PROXY_TARGET": api,
                "SCREEN_PROXY_SECRET": common["SCREEN_PROXY_SECRET"],
                "WEB_PORT": str(args.web_port),
                "RAKAZO_DEV_HOST": "127.0.0.1",
            },
            args.rakazo_root,
        )
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(120):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("A service exited; inspect the acceptance logs")
                try:
                    if (
                        client.get(api + "/health/ready").status_code == 200
                        and client.get(web).status_code == 200
                        and (pool / "pool.sock").exists()
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("Acceptance services did not become ready")
        print("P0: isolated PostgreSQL, API, Worker, Supervisor and original Web ready", flush=True)
        result = subprocess.run(
            [
                "node",
                "--import",
                str(args.rakazo_root / "node_modules/tsx/dist/loader.mjs"),
                str(args.browser_script.resolve()),
            ],
            cwd=args.repo,
            env={
                **base,
                "AGENTFORGE_RAKAZO_ROOT": str(args.rakazo_root),
                "AGENTFORGE_P0_FIXTURE": str(fixture),
                "AGENTFORGE_WEB_TEST_OUTPUT": str(args.output.resolve()),
            },
            check=False,
        )
        # Persist only counts/configuration without input, credentials or signed screen URLs.
        import sqlalchemy as sa
        from agentforge_storage.schema import runs, tool_calls

        with engine.connect() as conn:
            summary = {
                "model": model.model,
                "image": image,
                "database": "PostgreSQL 17",
                "runs": [
                    dict(r)
                    for r in conn.execute(sa.select(runs.c.run_id, runs.c.status)).mappings()
                ],
                "tools": [
                    dict(r)
                    for r in conn.execute(
                        sa.select(tool_calls.c.name, tool_calls.c.status)
                    ).mappings()
                ],
                "passed": result.returncode == 0,
            }
        (args.output / "backend-result.json").write_text(json.dumps(summary, indent=2))
        if result.returncode:
            raise SystemExit(result.returncode)
        print(
            "P0: browser acceptance passed; disposing test services and test database", flush=True
        )


if __name__ == "__main__":
    main()

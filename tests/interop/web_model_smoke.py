"""Opt-in real-model web acceptance; disposable SQLite, no development data or Computer."""

import argparse
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_core.models import Scope
from agentforge_core.settings import Settings
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.database import create_database
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import tool_calls
from agentforge_worker.worker import Worker
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("duckduckgo", "bing_rss"), default="duckduckgo")
    args = parser.parse_args()
    model = ModelSettings.from_env()
    if model.provider != "chat_completions":
        parser.error("Configure a real Chat Completions model explicitly before running")
    model = replace(model, max_rounds=8, timeout_seconds=120)
    # Capture only inference settings, then remove development DB, identity and tool grants.
    for name in list(os.environ):
        if name.startswith("AGENTFORGE_") or name == "SCREEN_PROXY_SECRET":
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix="af-web-acceptance-") as temporary:
        state = Path(temporary)
        database = f"sqlite+pysqlite:///{state / 'acceptance.db'}"
        engine = create_database(database)
        try:
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            scope = Scope("web-" + uuid4().hex, "acceptance", "alice")
            identities = IdentityRepository(engine)
            identities.bootstrap(
                scope, tenant_name="Web acceptance", space_name="Test", display_name="Alice"
            )
            token = identities.issue_token(scope, lifetime_seconds=600)["token"]
            policy = state / "web.json"
            policy.write_text(
                json.dumps(
                    {
                        "grants": [
                            {
                                "tenant_id": scope.tenant_id,
                                "space_id": scope.space_id,
                                "users": [scope.user_id],
                                "origins": ["*"],
                            }
                        ]
                    }
                )
            )
            os.environ.update(
                AGENTFORGE_WEB_ENABLED="true",
                AGENTFORGE_WEB_SEARCH_PROVIDER=args.provider,
                AGENTFORGE_WEB_POLICY_FILE=str(policy),
            )
            store = RunRepository(engine, auth_mode="database")
            settings = Settings(database_url=database, auth_mode="database")
            with TestClient(create_app(settings, store, model_settings=model)) as client:

                def rpc(name, data):
                    response = client.post(
                        "/rpc/" + name.replace(".", "/"),
                        headers={"Authorization": "Bearer " + token},
                        json={"json": data},
                    )
                    if response.status_code != 200:
                        raise RuntimeError(f"Acceptance RPC failed: {name}")
                    return response.json()["json"]

                bot = rpc("bots.create", {"name": "Web acceptance"})
                receipt = rpc(
                    "threads.send",
                    {
                        "botId": bot["id"],
                        "text": "请联网检索 Python 官方文档，打开一个 python.org 官方来源，"
                        "简短说明该网站提供哪类文档，并给出实际来源链接。",
                    },
                )
                worker = Worker(
                    store,
                    harness_factory=lambda snapshot: configured_harness(
                        model.for_snapshot(snapshot.model)
                    ),
                )
                worker.run_once()
                run = store.get(scope, receipt["runId"])
                with engine.connect() as connection:
                    records = connection.execute(
                        sa.select(tool_calls.c.name, tool_calls.c.status)
                        .where(tool_calls.c.run_id == run.run_id)
                        .order_by(tool_calls.c.sequence)
                    ).all()
                evidence = {
                    "model": model.model,
                    "searchProvider": args.provider,
                    "status": run.status.value,
                    "error": run.error_code,
                    "tools": [{"name": name, "status": status} for name, status in records],
                    "answer": run.result,
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
                args.output.chmod(0o600)
                succeeded = {name for name, status in records if status == "succeeded"}
                if (
                    run.status.value != "succeeded"
                    or not {"web_search", "web_fetch"} <= succeeded
                    or "https://" not in (run.result or "")
                ):
                    raise SystemExit("Real-model web acceptance failed; inspect evidence")
                thread = rpc("threads.get", {"botId": bot["id"]})
                if not all(name + "@1" in json.dumps(thread) for name in succeeded):
                    raise SystemExit("Missing original thread Activity projection")
                print("Real-model search, fetch, citations and original thread Activity passed.")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()

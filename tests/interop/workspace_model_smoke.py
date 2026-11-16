"""Opt-in real model/API/Worker/Docker acceptance in an isolated SQLite workspace.

Approves only workspace_save/open_path for this disposable test account.
No original Web browser UI or semantic visual review is claimed.
"""

import argparse
import base64
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import sqlalchemy as sa
from agentforge_core.models import Scope
from agentforge_runtime.config import ModelSettings
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_storage.database import create_database
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import approval_requests, external_effects, tool_calls
from alembic import command
from alembic.config import Config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="agentforge/desktop-preview:acceptance")
    args = parser.parse_args()
    model = ModelSettings.from_env()
    if model.provider != "chat_completions" or not os.environ.get("AGENTFORGE_MODEL_API_KEY"):
        parser.error("Configure a real model and key locally")
    python_image = os.environ.get("AGENTFORGE_PYTHON_IMAGE", "")
    if not python_image.startswith("sha256:"):
        parser.error("Configure an immutable Python image ID")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AGENTFORGE_") and k != "SCREEN_PROXY_SECRET"
    }
    base.update(DOCKER_CONTEXT="colima", PYTHONDONTWRITEBYTECODE="1")
    image = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
        env=base,
        text=True,
    ).strip()
    evidence = {"model": model.model, "image": image, "runs": [], "passed": False}
    try:
        with ExitStack() as resources:
            state = Path(
                resources.enter_context(tempfile.TemporaryDirectory(prefix="af-e2e-", dir="/tmp"))
            )
            (state / "slots").mkdir(mode=0o700)
            database = f"sqlite+pysqlite:///{state / 'test.db'}"
            engine = create_database(database)
            resources.callback(engine.dispose)
            config = Config("alembic.ini")
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            scope = Scope("acceptance-" + uuid4().hex, "workspace", "alice")
            identities = IdentityRepository(engine)
            identities.bootstrap(
                scope,
                tenant_name="Acceptance",
                space_name="Temporary",
                display_name="Alice",
            )
            token = identities.issue_token(scope, lifetime_seconds=3600)["token"]
            policy = state / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "grants": [
                            dict(
                                tenant_id=scope.tenant_id,
                                space_id=scope.space_id,
                                users=[scope.user_id],
                            )
                        ]
                    }
                )
            )
            api_port, compute_port = free_port(), free_port()
            api = f"http://127.0.0.1:{api_port}"
            common = dict(
                base,
                AGENTFORGE_AUTH_MODE="database",
                AGENTFORGE_DATABASE_URL=database,
                AGENTFORGE_DESKTOP_POOL=str(state / "slots/pool.sock"),
                AGENTFORGE_WORKSPACE_CHECKPOINTS=str(state / "checkpoints"),
                AGENTFORGE_BROWSER_API_ORIGIN=api,
                AGENTFORGE_TERMINAL_API_ORIGIN=api,
                AGENTFORGE_BROWSER_TOKEN=secrets.token_hex(32),
                SCREEN_PROXY_SECRET=secrets.token_hex(32),
                AGENTFORGE_COMPUTER_IMAGE=image,
                AGENTFORGE_DESKTOP_LIMIT="2",
                AGENTFORGE_DESKTOP_IDLE_SECONDS="600",
                AGENTFORGE_ALLOWED_TOOLS="example_report@1,text_stats@1",
                AGENTFORGE_WORKSPACE_OPEN_ENABLED="true",
                AGENTFORGE_WORKSPACE_POLICY_FILE=str(policy),
                AGENTFORGE_PYTHON_POLICY_FILE=str(policy),
                AGENTFORGE_PYTHON_IMAGE=python_image,
                AGENTFORGE_PYTHON_ENDPOINT=f"http://127.0.0.1:{compute_port}",
                AGENTFORGE_MCP_COMPUTER_TOKEN=secrets.token_hex(32),
            )
            real = {
                **common,
                **{k: v for k, v in os.environ.items() if k.startswith("AGENTFORGE_MODEL_")},
            }
            real.update(AGENTFORGE_MODEL_MAX_ROUNDS="20", AGENTFORGE_MODEL_TIMEOUT_SECONDS="120")
            processes = []

            def stop():
                for proc in reversed(processes):
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=100)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()

            resources.callback(stop)

            def start(label, argv, env):
                log = resources.enter_context((args.output / (label + ".log")).open("x"))
                processes.append(
                    subprocess.Popen(
                        argv,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )

            def server(module, port):
                return [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    module + ":create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ]

            start(
                "desktop",
                [sys.executable, "-m", "agentforge_supervisor.desktop_pool"],
                common,
            )
            start("compute", server("agentforge_supervisor.app", compute_port), common)
            start("api", server("agentforge_api.app", api_port), real)
            start(
                "worker",
                [sys.executable, "-m", "agentforge_worker.main", "--watch"],
                real,
            )
            with httpx.Client(
                base_url=api,
                headers={"Authorization": "Bearer " + token},
                timeout=20,
                trust_env=False,
            ) as client:

                def rpc(name, data):
                    r = client.post("/rpc/" + name.replace(".", "/"), json={"json": data})
                    if r.status_code != 200:
                        raise RuntimeError("RPC failed: " + name + " " + str(r.status_code))
                    return r.json()["json"]

                for _ in range(60):
                    try:
                        if any(proc.poll() is not None for proc in processes):
                            raise RuntimeError("Acceptance service exited")
                        if not (state / "slots/pool.sock").exists():
                            time.sleep(0.5)
                            continue
                        bot = rpc("bots.create", {"name": "Workspace acceptance"})
                        break
                    except (httpx.ConnectError, RuntimeError):
                        time.sleep(0.5)
                else:
                    raise RuntimeError("API not ready")
                store = RunRepository(engine, auth_mode="database")
                artifacts = ArtifactRepository(store)

                def turn(prompt):
                    rid = rpc("threads.send", {"botId": bot["id"], "text": prompt})["runId"]
                    deadline = time.monotonic() + 600
                    approved = set()
                    while time.monotonic() < deadline:
                        run = store.get(scope, rid)
                        if run.status.value in ("succeeded", "failed", "cancelled"):
                            break
                        if run.status.value == "waiting_input":
                            with engine.connect() as c:
                                cards = (
                                    c.execute(
                                        sa.select(approval_requests)
                                        .join(external_effects)
                                        .where(
                                            approval_requests.c.run_id == rid,
                                            external_effects.c.status == "pending",
                                        )
                                    )
                                    .mappings()
                                    .all()
                                )
                                calls = (
                                    c.execute(
                                        sa.select(tool_calls.c.name).where(
                                            tool_calls.c.run_id == rid
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                            if any(
                                n
                                not in {
                                    "python_execute",
                                    "skill_read",
                                    "workspace_save",
                                    "open_path",
                                    "list_files",
                                    "attach_file",
                                    "read_file",
                                    "text_stats",
                                    "example_report",
                                }
                                for n in calls
                            ):
                                raise RuntimeError("Unexpected tool in acceptance")
                            for card in cards:
                                if card["message_id"] not in approved:
                                    rpc(
                                        "threads.answer",
                                        dict(
                                            botId=bot["id"],
                                            runId=rid,
                                            messageId=card["message_id"],
                                            answer="allow",
                                        ),
                                    )
                                    approved.add(card["message_id"])
                        time.sleep(1)
                    else:
                        raise RuntimeError("Acceptance run timed out")
                    with engine.connect() as c:
                        rows = c.execute(
                            sa.select(tool_calls.c.name, tool_calls.c.status, tool_calls.c.output)
                            .where(tool_calls.c.run_id == rid)
                            .order_by(tool_calls.c.sequence)
                        ).all()
                    record = dict(
                        status=run.status.value,
                        error=run.error_code,
                        tools=[dict(name=n, status=s) for n, s, output in rows],
                        approvals=len(approved),
                    )
                    evidence["runs"].append(record)
                    print(json.dumps(record), flush=True)
                    previews = [
                        output for n, s, output in rows if n == "open_path" and s == "succeeded"
                    ]
                    record["previews"] = previews
                    if not previews or not all(p.get("previewed") is True for p in previews):
                        raise RuntimeError("Preview did not load")
                    if run.status.value != "succeeded":
                        raise RuntimeError("Model workflow failed")
                    successful = {n for n, s, output in rows if s == "succeeded"}
                    if (
                        not {"python_execute", "workspace_save", "open_path"} <= successful
                        or len(approved) < 2
                    ):
                        raise RuntimeError("Missing generation/save/preview or approvals")

                turn(
                    "请使用PPT生成技能生成一页中文PPT，文件名acceptance.pptx，标题为“第一版验收”，正文为“生成、保存、审批预览”。实际生成文件并发布附件，然后保存该产物到电脑，调用open_path展示预览。需要审批时等待。不要仅给大纲，也不要重复发布同一文件。"
                )
                first = artifacts.listing(scope, bot["id"], limit=20)["items"]
                if len(first) != 1:
                    raise RuntimeError("Expected one PPT artifact family")
                old = artifacts.get(scope, first[0]["id"])
                turn(
                    "继续修改刚才的PPT：以已有附件为输入，将标题“第一版验收”改为“第二版验收”，保留正文，使用相同输入文件名导出并发布新版附件；保存新版到电脑并调用open_path预览。不要重新创建无版本关系的文件。"
                )
                current = artifacts.listing(scope, bot["id"], limit=20)["items"]
                if len(current) != 1 or current[0]["versionCount"] != 2:
                    raise RuntimeError("Expected exactly two versions in one family")
                new = artifacts.get(scope, current[0]["id"])

                def xml(artifact):
                    raw = base64.b64decode(artifact["contentBase64"])
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:
                        return "".join(
                            z.read(n).decode()
                            for n in z.namelist()
                            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                        )

                if "第一版验收" not in xml(old) or "第二版验收" not in xml(new):
                    raise RuntimeError("PPT revision contents incorrect")
                if artifacts.get(scope, first[0]["id"])["contentBase64"] != old["contentBase64"]:
                    raise RuntimeError("Original bytes changed")
                evidence.update(passed=True, versions=2, original_preserved=True)
                print(
                    "Real model/API/Worker/Desktop two-version acceptance passed.",
                    flush=True,
                )
    finally:
        (args.output / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()

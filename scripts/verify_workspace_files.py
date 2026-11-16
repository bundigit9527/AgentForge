"""Opt-in real Docker desktop acceptance; uses disposable workspaces, no database or model."""

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentforge_supervisor.desktop import docker, running_desktop
from agentforge_supervisor.desktop_slot import execute_workspace


def require(condition, label):
    if not condition:
        raise RuntimeError("Acceptance failed: " + label)


def verify(mode, generation, container):
    bot, other = str(uuid4()), str(uuid4())
    checks = []

    def call(action, request, *, owner=bot):
        request = dict(request)
        if mode == "team":
            request["workspaceBot"] = owner
        return execute_workspace(action, request, generation)

    def result(action, request, *, owner=bot):
        reply = call(action, request, owner=owner)
        require("result" in reply, action + " returned " + str(reply.get("error")))
        return reply["result"]

    path = "reports/2026/方案.html"
    content = "<h1>项目方案</h1>\n" + "中文😀\n" * 600
    args = dict(path=path, content=content, expected_sha256=None)
    require(
        call("writeText", args).get("error") in {"file_missing", "workspace_file_missing"},
        "legacy directory refusal",
    )
    checks.append("legacy_directory_refusal")
    created = result("writeTextV2", args)
    digest = hashlib.sha256(content.encode()).hexdigest()
    require(created["sha256"] == digest, "nested Unicode write")
    checks.append("nested_unicode_write")
    collected, offset = "", 0
    while True:
        page = result(
            "readText",
            dict(path=path, offset=offset, limit=2000, expected_sha256=digest),
        )
        collected += page["content"]
        if page["next_offset"] is None:
            break
        require(page["next_offset"] > offset, "pagination progress")
        offset = page["next_offset"]
    require(collected == content, "paged read")
    checks.append("paged_read")
    for expected in (None, "0" * 64):
        reply = call(
            "writeTextV2",
            dict(path=path, content="must not overwrite", expected_sha256=expected),
        )
        require(reply.get("error") == "workspace_file_changed", "conflict refusal")
    checks.append("conflict_refusal")
    changed = "<h1>修订版</h1>"
    result("writeTextV2", dict(path=path, content=changed, expected_sha256=digest))
    raw = result("downloadFile", dict(path=path))
    require(
        base64.b64decode(raw["contentBase64"], validate=True) == changed.encode(),
        "download bytes",
    )
    checks.append("edit_and_download")
    listing = result("files", dict(path="reports/2026"))
    require(any(item["path"] == path for item in listing), "directory listing")
    checks.append("directory_listing")
    if mode == "team":
        require(
            call("readText", dict(path=path), owner=other).get("error")
            in {"file_missing", "workspace_file_missing"},
            "other Bot isolation",
        )
        result(
            "writeTextV2",
            dict(path=path, content="other bot", expected_sha256=None),
            owner=other,
        )
        require(
            result("readText", dict(path=path))["content"] == changed,
            "same-name Bot isolation",
        )
        checks.append("team_bot_path_isolation")
    # Operator fixture only: create a link pointing outside the authorized Bot directory.
    physical = f"/workspace/{bot}" if mode == "team" else "/workspace"
    docker(
        "exec",
        container,
        "python3",
        "-I",
        "-S",
        "-c",
        "import os,sys; os.symlink('/tmp', sys.argv[1]+'/outside')",
        physical,
    )
    reply = call(
        "writeTextV2",
        dict(path="outside/escape.txt", content="forbidden", expected_sha256=None),
    )
    require(reply.get("error") == "file_invalid", "symlink refusal")
    exists = docker(
        "exec",
        container,
        "python3",
        "-I",
        "-S",
        "-c",
        "import os; print(os.path.exists('/tmp/escape.txt'))",
    )
    require(exists == "False", "symlink target unchanged")
    checks.append("symlink_refusal")
    require(
        call("readText", dict(path="../escape")).get("error") == "file_invalid",
        "path traversal refusal",
    )
    checks.append("path_traversal_refusal")
    return dict(mode=mode, checks=checks, status="passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context",
        default="colima",
        help="Docker context; does not change global selection",
    )
    parser.add_argument("--image", default=os.environ.get("AGENTFORGE_COMPUTER_IMAGE", ""))
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New JSON evidence path; never overwritten",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image):
        parser.error("Provide an existing immutable desktop image ID using --image")
    if args.output.exists():
        parser.error("Evidence file already exists; choose a new path")
    os.environ["DOCKER_CONTEXT"] = args.context
    image = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    require(image == args.image, "image identity")

    def stop(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    results = []
    completed = False
    try:
        for mode in ("team", "dedicated"):
            generation = uuid4().hex
            identity = SimpleNamespace(tenant="acceptance", space="disposable", user="test")
            with tempfile.TemporaryDirectory(prefix="agentforge-file-acceptance-") as temporary:
                manifest = Path(temporary) / "desktop.json"
                with running_desktop(
                    identity, str(uuid4()), image, manifest, generation
                ) as container:
                    results.append(verify(mode, generation, container))
            remaining = docker(
                "ps", "-aq", "--filter", "name=^/agentforge-desktop-" + generation + "$"
            )
            require(not remaining, "temporary desktop cleanup")
        completed = True
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(
                dict(
                    image=image,
                    context=args.context,
                    results=results,
                    passed=completed,
                    scope=(
                        "Real desktop file protocol only; no model, API approvals, "
                        "database, rendering or persistence acceptance"
                    ),
                ),
                stream,
                ensure_ascii=False,
                indent=2,
            )
            stream.write("\n")
    print("Workspace desktop acceptance passed; evidence: " + str(args.output))


if __name__ == "__main__":
    main()

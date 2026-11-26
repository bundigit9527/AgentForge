"""Real Chromium forms using deterministic HTML; no public site, database or model calls.

DOCKER_CONTEXT=colima uv run python tests/interop/browser_forms_smoke.py \
    --image agentforge/desktop-preview:acceptance --output /tmp/agentforge-forms-check
"""

import argparse
import base64
import hmac
import html
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from agentforge_supervisor import browser_pool
from agentforge_supervisor.browser_fetch import CSP
from agentforge_supervisor.browser_pool import BrowserPool
from agentforge_supervisor.desktop import running_desktop

ORIGIN = "https://forms.example.test"
FORM = """<h1>搜索验收 / Search</h1><form action="/results" method="get" target="_blank">
<label>关键词<input name="q" type="search" maxlength="200" required></label>
<label>备注<textarea name="note"></textarea></label>
<input type="hidden" name="tag" value="first"><input type="hidden" name="tag" value="second">
<input name="fixed" readonly value="readonly"><input name="ignored" disabled value="disabled">
<button type="submit" name="source" value="visible">搜索</button></form>
<form method="post"><input name="never"><button>Post</button></form>
<form action="https://other.example.test/"><input name="remote"><button>Cross-origin</button></form>
<form><input type="password" value="private-not-returned"><button>Login</button></form>
<script>document.body.append('SCRIPT-EXECUTED')</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    token = "forms-smoke-" + uuid4().hex
    os.environ["AGENTFORGE_BROWSER_TOKEN"] = token
    revoked = threading.Event()
    fetched = []

    def fixture(url, origins):
        assert url.startswith(ORIGIN + "/"), url
        fetched.append(url)
        if urlsplit(url).path == "/results":
            content = (
                "<h1>检索结果</h1><pre>"
                + html.escape(json.dumps(parse_qs(urlsplit(url).query), ensure_ascii=False))
                + "</pre>"
            )
        elif urlsplit(url).path == "/clobber":
            content = """<form action="/results"><input name="action" value="safe">
            <input name="elements" value="safe"><input name="checkValidity" value="safe">
            <input name="target" value="safe"><button>Submit</button></form>"""
        else:
            content = FORM
        body = (
            '<html lang="zh"><meta charset="utf-8"><title>Form acceptance</title>'
            "<style>body{font:22px sans-serif;padding:36px;background:#f2f6ff}"
            "label{display:block;margin:24px}input,textarea,button{font:inherit}</style>"
            + content
            + "</html>"
        )
        return dict(
            status=200,
            body=base64.b64encode(body.encode()).decode(),
            headers=[
                {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                {"name": "Content-Security-Policy", "value": CSP},
            ],
        )

    browser_pool.fetch_document = fixture  # Test-only broker; production network checks unchanged.

    class Check(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            ok = not revoked.is_set() and hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            )
            self.send_response(200 if ok else 403)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Check)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["AGENTFORGE_BROWSER_API_ORIGIN"] = f"http://127.0.0.1:{server.server_port}"
    checks = []
    try:
        image = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
        ).strip()
        generation = uuid4().hex
        pool = BrowserPool()
        with tempfile.TemporaryDirectory(prefix="af-forms-") as folder:
            with running_desktop(
                SimpleNamespace(tenant="forms-test", space="test", user="tester"),
                str(uuid4()),
                image,
                Path(folder) / "manifest.json",
                generation,
            ) as name:
                run, attempt = "first-user-run", "first-attempt"

                def request(action, **data):
                    return pool.execute(
                        generation,
                        dict(
                            runId=run,
                            attemptId=attempt,
                            operationId="test-op",
                            origins=[ORIGIN],
                            formOrigins=[ORIGIN],
                            operation=dict(action=action, **data),
                        ),
                    )

                def perform(action, **data):
                    reply = request(action, **data)
                    assert "result" in reply, (action, reply)
                    return reply["result"]

                def action(page, actions):
                    return perform(
                        "act", url=page["url"], snapshot=page["snapshot"], actions=actions
                    )

                def screenshot(filename):
                    with (args.output / filename).open("wb") as stream:
                        subprocess.run(
                            [
                                "docker",
                                "exec",
                                "-e",
                                "DISPLAY=:1",
                                name,
                                "import",
                                "-window",
                                "root",
                                "png:-",
                            ],
                            stdout=stream,
                            check=True,
                            timeout=15,
                        )

                def mutate(expression):
                    target = pool.states[generation]["state"]["target"]
                    program = (
                        "import runpy;d=runpy.run_path('/usr/local/bin/rakazo-page-browser');"
                        "c=d['CdpClient'](d['discover_ws_url'](d['cdp_port']()));c.connect();"
                        "s=c.call('Target.attachToTarget',"
                        + repr(dict(targetId=target, flatten=True))
                        + ")[\"sessionId\"];c.call('Runtime.evaluate',"
                        + repr(dict(expression=expression))
                        + ",session_id=s);c.close()"
                    )
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-e",
                            "HOME=/tmp/home",
                            "-e",
                            "DISPLAY=:1",
                            name,
                            "python3",
                            "-I",
                            "-c",
                            program,
                        ],
                        check=True,
                        timeout=10,
                    )

                perform("open", url=ORIGIN + "/search")
                page = perform("snapshot")
                assert len(page["elements"]) == 3, page
                assert "private-not-returned" not in json.dumps(page)
                assert "SCRIPT-EXECUTED" not in page["text"]
                checks.append("supported elements only; no password values or page scripts")
                field, note, button = [e["ref"] for e in page["elements"]]
                assert (
                    request(
                        "act",
                        url=page["url"],
                        snapshot=page["snapshot"],
                        actions=[
                            dict(kind="fill", ref=field, text="must not partially apply"),
                            dict(kind="click", ref="f1e1"),
                        ],
                    )["error"]
                    == "browser_action_unsupported"
                )
                page = perform("snapshot")
                assert page["elements"][0]["value"] == ""
                checks.append("invalid batch has no partial fill or submission")
                attempt = "approved-attempt"
                page = action(page, [dict(kind="fill", ref=field, text="公司 AI")])
                assert page["elements"][0]["value"] == "公司 AI" and len(fetched) == 1
                checks.append("approved action continues same Run across new Attempt")
                page = action(
                    page,
                    [
                        dict(kind="type", ref=field, text=" 平台😀"),
                        dict(kind="fill", ref=note, text="第一行\nsecond line"),
                    ],
                )
                screenshot("filled.png")
                assert page["elements"][0]["value"] == "公司 AI 平台😀"
                checks.append("visible fill replaces; type appends; multiline Unicode preserved")
                page = action(page, [dict(kind="click", ref=button)])
                query = parse_qs(urlsplit(page["url"]).query)
                assert query == dict(
                    q=["公司 AI 平台😀"],
                    note=["第一行\r\nsecond line"],
                    tag=["first", "second"],
                    fixed=["readonly"],
                    source=["visible"],
                ), query
                assert "检索结果" in page["text"] and len(fetched) == 2
                screenshot("submitted.png")
                checks.append(
                    "native GET includes duplicates, hidden/readonly fields and submitter"
                )
                perform("open", url=ORIGIN + "/search")
                page = perform("snapshot")
                mutate("document.querySelector('input[name=q]').value='manual edit'")
                assert (
                    request(
                        "act",
                        url=page["url"],
                        snapshot=page["snapshot"],
                        actions=[dict(kind="fill", ref=field, text="stale")],
                    )["error"]
                    == "browser_page_changed"
                )
                page = perform("snapshot")
                assert page["elements"][0]["value"] == "manual edit"
                checks.append("manual changes invalidate approval snapshot; refresh observes edit")
                assert (
                    request(
                        "act",
                        url=page["url"],
                        snapshot=page["snapshot"],
                        actions=[dict(kind="fill", ref=field, text="x" * 201)],
                    )["error"]
                    == "browser_action_unsupported"
                )
                checks.append("maxlength rejected before changing input")
                page = perform("snapshot")
                run, attempt = "second-user-run", "second-attempt"
                assert request("snapshot")["error"] == "browser_page_changed"
                assert (
                    request(
                        "act",
                        url=page["url"],
                        snapshot=page["snapshot"],
                        actions=[dict(kind="click", ref=button)],
                    )["error"]
                    == "browser_page_changed"
                )
                perform("open", url=ORIGIN + "/search")
                page = perform("snapshot")
                assert page["elements"][0]["value"] == ""
                checks.append("different Run cannot reuse page, refs or filled values")
                perform("open", url=ORIGIN + "/clobber")
                page = perform("snapshot")
                assert len(page["elements"]) == 5
                result = action(
                    page,
                    [
                        dict(kind="fill", ref=page["elements"][0]["ref"], text="safe2"),
                        dict(kind="click", ref=page["elements"][-1]["ref"]),
                    ],
                )
                assert parse_qs(urlsplit(result["url"]).query)["action"] == ["safe2"]
                checks.append("named fields do not shadow trusted form methods")
                revoked.set()
                assert request("snapshot")["error"] == "browser_not_allowed"
                checks.append("revoked execution cannot continue")
        (args.output / "result.json").write_text(
            json.dumps(
                dict(
                    checks=checks,
                    count=len(checks),
                    database="not used",
                    model="not used",
                    browser="real Chromium in isolated Computer",
                    web="deterministic test broker",
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"PASS: {len(checks)} real Chromium form checks", flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Real CDP reading smoke using local HTML; no external web, DB or model requests.

DOCKER_CONTEXT=colima uv run python tests/interop/browser_reading_smoke.py \
    --image rakazo/computer:local --output /tmp/agentforge-browser-check
"""

import argparse
import base64
import hmac
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentforge_supervisor import browser_pool
from agentforge_supervisor.browser_fetch import CSP
from agentforge_supervisor.browser_pool import BrowserPool
from agentforge_supervisor.desktop import running_desktop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Existing local Computer image")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    token = "browser-smoke-" + uuid4().hex
    os.environ["AGENTFORGE_BROWSER_TOKEN"] = token
    revoked = threading.Event()

    def fixture_document(url, origins):
        assert url.startswith("https://reading.example.test/")
        if url.endswith("/long"):
            body = "A" * 200100
        elif "/link/" in url:
            body = "Destination " + url.rsplit("/", 1)[-1]
        else:
            body = "".join(f"<p>Row {i} 中文😀正文 " + "内容 " * 20 + "</p>" for i in range(300))
            body += "<h2>页面尾部精确定位</h2>"
            body += "".join(f'<p><a href="/link/{i}">Link {i}</a></p>' for i in range(1, 81))
        html = '<html lang="zh"><meta charset="utf-8"><title>Reading fixture</title><body>'
        html += body + "</body></html>"
        return {
            "status": 200,
            "body": base64.b64encode(html.encode()).decode(),
            "headers": [
                {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                {"name": "Content-Security-Policy", "value": CSP},
            ],
        }

    # Explicit test-only broker; production TLS/DNS policy remains unchanged.
    browser_pool.fetch_document = fixture_document

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
    try:
        image = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
        ).strip()
        generation = uuid4().hex
        pool = BrowserPool()
        with tempfile.TemporaryDirectory(prefix="af-browser-") as folder:
            with running_desktop(
                SimpleNamespace(tenant="browser-test", space="test", user="tester"),
                str(uuid4()),
                image,
                Path(folder) / "manifest.json",
                generation,
            ) as name:

                def request(action, **data):
                    return pool.execute(
                        generation,
                        dict(
                            runId="run",
                            attemptId="attempt",
                            operationId="operation",
                            origins=["*"],
                            operation=dict(action=action, **data),
                        ),
                    )

                def perform(action, **data):
                    result = request(action, **data)
                    assert "result" in result, result
                    page = result["result"]
                    print(action, page["url"], page["title"], flush=True)
                    return page

                first = perform("open", url="https://reading.example.test/main")
                read = perform("read", snapshot=first["snapshot"])
                assert read["total_chars"] > 10000 and read["next_offset"] == 4000
                second = perform("read", snapshot=read["snapshot"], offset=4000, links_offset=12)
                assert second["offset"] == 4000 and second["links"][0]["id"] == "13"
                assert request("read", snapshot=read["snapshot"])["error"] == "browser_page_changed"
                found = perform("find", snapshot=second["snapshot"], text="页面尾部精确定位")
                assert found["match_offset"] > 4000 and found["scroll_y"] > 0
                assert "页面尾部精确定位" in found["text"]
                top = perform("scroll", snapshot=found["snapshot"], direction="top")
                assert top["scroll_y"] == 0
                down = perform("scroll", snapshot=top["snapshot"], direction="down")
                assert down["scroll_y"] > 0
                bottom = perform("scroll", snapshot=down["snapshot"], direction="bottom")
                assert (
                    bottom["scroll_y"] + bottom["viewport_height"] >= bottom["document_height"] - 2
                )
                more = perform("read", snapshot=bottom["snapshot"], links_offset=60)
                assert more["links"][0]["id"] == "61"
                clicked = perform("follow", snapshot=more["snapshot"], link="61")
                assert clicked["url"].endswith("/link/61") and "Destination 61" in clicked["text"]
                # Simulate a human changing the page between observations.
                state = pool.states[generation]["state"]
                mutation = (
                    "import runpy; d=runpy.run_path('/usr/local/bin/rakazo-page-browser'); "
                    "c=d['CdpClient'](d['discover_ws_url'](d['cdp_port']()));c.connect();"
                    "s=c.call('Target.attachToTarget',{'targetId':"
                    + repr(state["target"])
                    + ",'flatten':True})['sessionId'];"
                    "c.call('Runtime.evaluate', "
                    + repr({"expression": "document.body.append(' changed')"})
                    + ", session_id=s);c.close()"
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
                        mutation,
                    ],
                    check=True,
                    timeout=10,
                )
                assert (
                    request("read", snapshot=clicked["snapshot"])["error"] == "browser_page_changed"
                )
                refreshed = perform("observe")
                assert "changed" in perform("read", snapshot=refreshed["snapshot"])["text"]
                long = perform("open", url="https://reading.example.test/long")
                limited = perform("read", snapshot=long["snapshot"], offset=199000)
                assert limited["truncated"] and limited["total_chars"] == 200000
                assert len(limited["text"]) == 1000 and limited["next_offset"] is None
                again = perform("open", url="https://reading.example.test/main")
                perform("find", snapshot=again["snapshot"], text="页面尾部精确定位")
                # Capture the whole X display, including visible Chromium; no fabricated DOM.
                with (args.output / "desktop.png").open("wb") as out:
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
                        stdout=out,
                        check=True,
                        timeout=15,
                    )
                revoked.set()
                assert request("observe") == {"error": "browser_not_allowed"}
                (args.output / "result.json").write_text(
                    json.dumps(
                        {
                            "urls": [first["url"], clicked["url"]],
                            "reading_checked": True,
                            "revoked_callback": True,
                            "database": "not used",
                            "model": "not used",
                        },
                        indent=2,
                    )
                )
                print("PASS: reading, find, scroll, link 61, stale recovery, cap and revocation")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

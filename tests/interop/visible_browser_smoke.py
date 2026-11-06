"""Opt-in real Docker/CDP smoke. No application DB or model requests.

DOCKER_CONTEXT=colima uv run python tests/interop/visible_browser_smoke.py \
    --image rakazo/computer:local --output /tmp/agentforge-browser-check
"""

import argparse
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

                first = perform("open", url="https://example.com/")
                assert "Example" in first["title"]
                second = perform("open", url="https://www.python.org/")
                assert "Python" in second["title"]
                observed = perform("observe")
                link = next(x for x in observed["links"] if x["text"] == "PSF")
                clicked = perform("follow", snapshot=observed["snapshot"], link=link["id"])
                assert clicked["url"] != observed["url"]
                assert "Foundation" in clicked["title"]
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
                            "urls": [first["url"], second["url"], clicked["url"]],
                            "revoked_callback": True,
                            "database": "not used",
                            "model": "not used",
                        },
                        indent=2,
                    )
                )
                print("PASS: visible navigation, observation, link click and callback revocation")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Execute bounded visible-browser steps in an assigned Computer, with API lease callbacks."""

import json
import selectors
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from agentforge_connectors.browser import service_configuration
from agentforge_core.browser import READING_ACTIONS, BrowserInput, browser_result, permitted_url

from agentforge_supervisor.browser_fetch import fetch_document


class BrowserDenied(Exception):
    pass


class BrowserPageChanged(Exception):
    pass


class BrowserPool:
    def __init__(self):
        self.states = {}

    def forget(self, generation):
        self.states.pop(generation, None)

    def execute(self, generation, request):
        identity = (request["runId"], request["attemptId"])
        operation = BrowserInput.model_validate(request["operation"])
        origins = request["origins"]
        state = self.states.get(generation, {})
        same = state.get("identity") == identity
        known_page = state.get("state") if operation.action == "observe" else state.get("page")
        if operation.action != "open" and (not same or not known_page):
            return {"error": "browser_page_changed"}
        if operation.action in READING_ACTIONS and operation.snapshot != state.get("snapshot"):
            return {"error": "browser_page_changed"}
        if operation.action == "open" and not permitted_url(operation.url, origins):
            return {"error": "browser_not_allowed"}
        href = None
        if operation.action == "follow":
            if operation.snapshot != state.get("snapshot"):
                return {"error": "browser_page_changed"}
            href = next(
                (x["url"] for x in state["page"]["links"] if x["id"] == operation.link), None
            )
            if href is None:
                return {"error": "browser_page_changed"}
        previous = state.get("state")
        config = {
            "operation": operation.model_dump(),
            "href": href,
            "state": previous if same else None,
            "closeContext": previous.get("context") if previous else None,
        }
        # Failed or interrupted navigation invalidates the old snapshot immediately.
        saved = {"identity": identity, "state": previous, "page": None}
        self.states[generation] = saved
        origin, token = service_configuration()
        with httpx.Client(timeout=3, trust_env=False) as client:

            def check():
                response = client.post(
                    origin + "/api/internal/browser/check",
                    headers={"Authorization": "Bearer " + token},
                    json={"operationId": request["operationId"]},
                )
                if response.status_code != 200:
                    raise BrowserDenied()

            try:
                check()
                page, new_state = self.drive(generation, config, origins, check, saved)
                check()
                if not permitted_url(page["url"], origins):
                    raise BrowserDenied()
                links = []
                for item in page["links"]:
                    try:
                        allowed = permitted_url(item["url"], origins)
                    except ValueError:
                        allowed = False
                    if allowed and len(item["url"]) <= 512:
                        links.append(item)
                        if len(links) == 12:
                            break
                page = browser_result(
                    operation.action, {**page, "links": links, "snapshot": uuid4().hex}
                )
                saved.update(state=new_state, page=page, snapshot=page["snapshot"])
                return {"result": page}
            except BrowserPageChanged:
                return {"error": "browser_page_changed"}
            except BrowserDenied:
                return {"error": "browser_not_allowed"}
            except Exception:
                return {"error": "browser_failed"}

    def drive(self, generation, config, origins, check, saved):
        name = "agentforge-desktop-" + generation
        args = ["docker", "exec", "-e", "HOME=/tmp/home", "-e", "DISPLAY=:1"]
        subprocess.run(
            [
                *args,
                "-d",
                name,
                "sh",
                "-c",
                "curl -fsS http://127.0.0.1:9222/json/version >/dev/null || "
                "exec rakazo-browser about:blank >/tmp/browser.log 2>&1",
            ],
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        program = Path(__file__).with_name("browser_tab.py").read_text()
        proc = subprocess.Popen(
            [*args, "-i", name, "timeout", "24", "python3", "-I", "-c", program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def send(value):
            proc.stdin.write((json.dumps(value) + "\n").encode())
            proc.stdin.flush()

        deadline = time.monotonic() + 24
        documents = 0
        try:
            send(config)
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                for _ in range(200):
                    if not selector.select(max(0, deadline - time.monotonic())):
                        raise RuntimeError("Browser timed out")
                    line = proc.stdout.readline(128001)
                    if not line.endswith(b"\n"):
                        raise RuntimeError("Invalid browser response")
                    message = json.loads(line)
                    check()
                    if message.get("error") == "browser_page_changed":
                        raise BrowserPageChanged()
                    if "session" in message:
                        saved["state"] = message["session"]
                        send({"ok": True})
                    elif "check" in message:
                        send({"ok": True})
                    elif "request" in message:
                        documents += 1
                        req = message["request"]
                        if (
                            documents > 5
                            or req["method"] != "GET"
                            or not permitted_url(req["url"], origins)
                        ):
                            raise BrowserDenied()
                        document = fetch_document(req["url"], origins)
                        check()
                        send(document)
                    elif "page" in message:
                        return message["page"], message["state"]
                    else:
                        raise RuntimeError("Browser page changed or failed")
                raise RuntimeError("Browser event budget exceeded")
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()

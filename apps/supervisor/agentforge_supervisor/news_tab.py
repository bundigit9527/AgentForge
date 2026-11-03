"""Fixed CDP workflow executed inside the assigned Computer image, over stdin/stdout."""

import json
import os
import re
import runpy
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HOME = "https://www.chinanews.com.cn/finance/"


def exchange(message):
    print(json.dumps(message), flush=True)
    line = sys.stdin.buffer.readline(1550000)
    if not line.endswith(b"\n"):
        raise RuntimeError("Screen broker disconnected")
    return json.loads(line)


def main():
    config = json.loads(input())
    driver = runpy.run_path("/usr/local/bin/rakazo-page-browser")
    port = driver["cdp_port"]()
    driver["wait_for_cdp"](port)
    cdp = driver["CdpClient"](driver["discover_ws_url"](port))
    cdp.connect()
    previous = config.get("target")
    live = cdp.call("Target.getTargets")["targetInfos"]
    reusable = any(
        t["targetId"] == previous and t["url"].startswith("https://www.chinanews.com.cn/")
        for t in live
    )
    if reusable:
        target = previous
    else:
        context = cdp.call("Target.createBrowserContext")["browserContextId"]
        target = cdp.call(
            "Target.createTarget", {"url": "about:blank", "browserContextId": context}
        )["targetId"]
    session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]

    def call(method, params=None):
        return cdp.call(method, params, session_id=session, timeout_s=12)

    call("Page.enable")
    call("Page.bringToFront")
    # Only top-level documents are brokered. Sources cannot run script, send forms,
    # load other origins, or access the user's authenticated browser profile.
    call("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]})
    loaded = {}
    original_event = cdp._handle_event

    def handle_event(event):
        if event.get("method") != "Fetch.requestPaused" or event.get("sessionId") != session:
            return original_event(event)
        params = event["params"]
        request = params["request"]
        response = exchange({"url": request["url"], "method": request["method"]})
        loaded[request["url"]] = response
        call(
            "Fetch.fulfillRequest",
            {
                "requestId": params["requestId"],
                "responseCode": 200,
                "responseHeaders": response["headers"],
                "body": response["body"],
            },
        )

    cdp._handle_event = handle_event

    def evaluate(expression):
        reply = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if reply.get("exceptionDetails"):
            raise RuntimeError("Unable to read publisher page")
        return reply["result"].get("value")

    def until(expression):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            value = evaluate(expression)
            if value:
                return value
            time.sleep(0.15)
        raise RuntimeError("Publisher page did not load")

    call("Page.navigate", {"url": HOME})
    until(
        "location.href === "
        + json.dumps(HOME)
        + " && !!document.querySelector('#newlist .news_title a')"
    )
    items = evaluate("""Array.from(document.querySelectorAll('#newlist li')).slice(0,100).map(li=>{
      const a=li.querySelector('.news_title a'), p=li.querySelector('.news_content p');
      return [a?.innerText?.trim(),a?.href,p?.innerText?.trim()];
    })""")
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    matched = {}
    for title, link, date in items:
        if (
            not title
            or len(title) > 200
            or not re.fullmatch(
                r"https://www\.chinanews\.com\.cn/[\w-]+/\d{4}/\d{2}-\d{2}/\d+\.shtml", link or ""
            )
        ):
            continue
        try:
            when = datetime.strptime(date or "", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if (
            when.date() == now.date()
            and when <= now
            and config["keyword"].casefold() in title.casefold()
        ):
            matched[link] = (when, title, link)
    rows = sorted(matched.values(), reverse=True)[:5]
    if rows:
        link = rows[0][2]
        anchor = (
            "Array.from(document.querySelectorAll('#newlist .news_title a')).find(a=>a.href==="
            + json.dumps(link)
            + ")"
        )
        evaluate(f"({anchor}).scrollIntoView({{block:'center'}})")
        # Let the viewer see the located link before following it in the same tab.
        time.sleep(1)
        evaluate(f"(()=>{{let a={anchor};a.target='_self';a.click();}})()")
        until(
            "location.href==="
            + json.dumps(link)
            + " && document.readyState!=='loading' && document.body.innerText.trim().length>40"
        )
    opened = evaluate("location.href")
    if rows:
        until(
            "document.body.innerText.replace(/\\s/g,'').includes("
            + json.dumps("".join(rows[0][1].split()))
            + ")"
        )
        evaluate("""Array.from(document.querySelectorAll('h1')).at(-1)
                    ?.scrollIntoView({block:'start'})""")
    if opened not in loaded:
        raise RuntimeError("No verified publisher response for the visible page")
    # Keep one guest-only copy of the final document for reloads. The child has no
    # broker pipes, no credentials and no egress. It relinquishes the target on
    # navigation away (including the next search), or exits when its tab closes.
    if os.fork() == 0:
        os.setsid()
        fd = os.open(os.devnull, os.O_RDWR)
        for standard in (0, 1, 2):
            os.dup2(fd, standard)
        os.close(fd)
        try:
            cdp._handle_event = original_event
            cdp._sock.settimeout(60)
            while True:
                try:
                    event = json.loads(cdp._read_frame())
                except TimeoutError:
                    continue
                if event.get("sessionId") != session:
                    if event.get("params", {}).get("sessionId") == session:
                        break
                    continue
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event["params"]
                req = params["request"]
                if req["url"] != opened or req["method"] != "GET":
                    call("Fetch.continueRequest", {"requestId": params["requestId"]})
                    break
                response = loaded[opened]
                call(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": params["requestId"],
                        "responseCode": 200,
                        "responseHeaders": response["headers"],
                        "body": response["body"],
                    },
                )
        finally:
            os._exit(0)
    print(
        json.dumps(
            {
                "target": target,
                "date": str(now.date()),
                "items": [[dt.strftime("%H:%M"), title, url] for dt, title, url in rows],
                "opened": opened,
            }
        ),
        flush=True,
    )
    cdp.close()


if __name__ == "__main__":
    main()

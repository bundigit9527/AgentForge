"""Fixed Python CDP driver copied into the read-only Computer. Model supplies data, never code."""

import hashlib
import json
import runpy
import sys
import time

OBSERVE = """(()=>({url:location.href,title:document.title.slice(0,200),
 text:(document.body?.innerText||'').slice(0,4000),
 links:Array.from(document.querySelectorAll('a[href]'))
 .filter(a=>a.getClientRects().length>0 && a.href.length<=2048)
 .slice(0,60).map((a,i)=>({id:String(i+1),text:(a.innerText||a.title||'').trim().slice(0,80),url:a.href}))
 }))()"""


READ_DOCUMENT = """(()=>{
 const chars=Array.from(document.body?.innerText||'');
 const links=Array.from(document.querySelectorAll('a[href]'))
   .filter(a=>a.getClientRects().length>0 && a.href.length<=2048);
 return {url:location.href,title:document.title.slice(0,200),
 text:chars.slice(0,200000).join(''),
 links:links.slice(0,480).map((a,i)=>({id:String(i+1),
 text:Array.from((a.innerText||a.title||'').trim()).slice(0,80).join(''),url:a.href})),
 truncated:chars.length>200000||links.length>480,
 scroll_y:Math.max(0,Math.round(scrollY)),viewport_height:Math.max(1,innerHeight),
 document_height:Math.max(0,document.documentElement.scrollHeight)};
 })()"""


def document_hash(document):
    stable = {k: document[k] for k in ("url", "title", "text", "links", "truncated")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def reading_page(document, operation):
    text, links = document["text"], document["links"]
    offset = min(operation.get("offset") or 0, len(text))
    link_offset = min(operation.get("links_offset") or 0, len(links))
    match = None
    if operation["action"] == "find":
        index = text.find(operation["text"])
        if index >= 0:
            match = index
            offset = max(0, index - 400)
    end, link_end = min(offset + 4000, len(text)), min(link_offset + 12, len(links))
    return {
        **document,
        "text": text[offset:end],
        "links": links[link_offset:link_end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text),
        "links_offset": link_offset,
        "next_link_offset": link_end if link_end < len(links) else None,
        "total_links": len(links),
        "match_offset": match,
    }


def exchange(value):
    print(json.dumps(value), flush=True)
    line = sys.stdin.buffer.readline(1550000)
    if not line.endswith(b"\n"):
        raise RuntimeError("Browser broker disconnected")
    return json.loads(line)


def main():
    config = json.loads(input())

    def check():
        if exchange({"check": True}) != {"ok": True}:
            raise RuntimeError("Execution revoked")

    check()
    driver = runpy.run_path("/usr/local/bin/rakazo-page-browser")
    port = driver["cdp_port"]()
    driver["wait_for_cdp"](port)
    cdp = driver["CdpClient"](driver["discover_ws_url"](port))
    cdp.connect()
    try:
        state = config.get("state")
        if state:
            context, target = state["context"], state["target"]
        else:
            if config.get("closeContext"):
                try:
                    cdp.call(
                        "Target.disposeBrowserContext", {"browserContextId": config["closeContext"]}
                    )
                except Exception:
                    pass
            context = cdp.call("Target.createBrowserContext")["browserContextId"]
            target = cdp.call(
                "Target.createTarget", {"url": "about:blank", "browserContextId": context}
            )["targetId"]
        session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]

        def call(method, params=None):
            check()
            return cdp.call(method, params, session_id=session, timeout_s=8)

        exchange({"session": {"context": context, "target": target}})
        call("Page.enable")
        call("Page.bringToFront")
        call("Page.setDownloadBehavior", {"behavior": "deny"})
        frame = call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        navigated = False
        original = cdp._handle_event

        def event(message):
            nonlocal navigated
            if (
                message.get("method") in {"Page.frameNavigated", "Page.navigatedWithinDocument"}
                and message.get("sessionId") == session
                and message["params"].get("frame", {}).get("id", message["params"].get("frameId"))
                == frame
            ):
                navigated = True
            if (
                message.get("method") != "Fetch.requestPaused"
                or message.get("sessionId") != session
            ):
                return original(message)
            params = message["params"]
            req = params["request"]
            if (
                params.get("resourceType") != "Document"
                or params.get("frameId") != frame
                or req["method"] != "GET"
            ):
                return cdp.call(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                    session_id=session,
                )
            reply = exchange({"request": {"url": req["url"], "method": req["method"]}})
            return cdp.call(
                "Fetch.fulfillRequest",
                {
                    "requestId": params["requestId"],
                    "responseCode": reply["status"],
                    "responseHeaders": reply["headers"],
                    "body": reply["body"],
                },
                session_id=session,
            )

        cdp._handle_event = event
        call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

        def evaluate(expression):
            result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            if result.get("exceptionDetails"):
                raise RuntimeError("Cannot observe page")
            return result["result"].get("value")

        def observe():
            return evaluate(OBSERVE)

        def fingerprint(page):
            return hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()

        operation = config["operation"]
        extended = operation["action"] in {"read", "scroll", "find"}
        if operation["action"] == "open":
            call("Page.navigate", {"url": operation["url"]})
        else:
            page = observe()
            document = evaluate(READ_DOCUMENT)
            changed = (extended and document_hash(document) != state.get("documentHash")) or (
                operation["action"] == "follow"
                and (
                    fingerprint(page) != state.get("hash")
                    or (
                        state.get("documentHash")
                        and document_hash(document) != state["documentHash"]
                    )
                )
            )
            if changed:
                print(json.dumps({"error": "browser_page_changed"}), flush=True)
                return
            if operation["action"] == "scroll":
                actions = {
                    "up": "window.scrollBy(0,-innerHeight*0.85)",
                    "down": "window.scrollBy(0,innerHeight*0.85)",
                    "top": "window.scrollTo(0,0)",
                    "bottom": "window.scrollTo(0,document.documentElement.scrollHeight)",
                }
                evaluate(actions[operation["direction"]])
            if operation["action"] == "find" and operation["text"] in document["text"]:
                found = evaluate(
                    "(()=>{const s=window.getSelection(),r=document.createRange();"
                    "r.selectNodeContents(document.body);r.collapse(true);"
                    "s.removeAllRanges();s.addRange(r);return window.find("
                    + json.dumps(operation["text"])
                    + ",true,false,false,false,false,false);})()"
                )
                if not found:
                    raise RuntimeError("Could not locate visible text")
            if operation["action"] == "follow":
                href = config["href"]
                expression = (
                    "Array.from(document.querySelectorAll('a[href]')).find(a=>a.href==="
                    + json.dumps(href)
                    + ")"
                )
                evaluate(f"({expression}).scrollIntoView({{block:'center'}})")
                check()
                evaluate(f"(()=>{{const a={expression};a.target='_self';a.click();}})()")
        deadline = time.monotonic() + 8
        while True:
            loading = evaluate("document.readyState==='loading'||location.href==='about:blank'")
            if not loading and (operation["action"] == "observe" or extended or navigated):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Page not loaded")
            time.sleep(0.1)
        page = observe()
        document = evaluate(READ_DOCUMENT)
        check()
        result = reading_page(document, operation) if extended else page
        print(
            json.dumps(
                {
                    "page": result,
                    "state": {
                        "context": context,
                        "target": target,
                        "hash": fingerprint(page),
                        "documentHash": document_hash(document),
                    },
                }
            ),
            flush=True,
        )
    finally:
        cdp.close()


if __name__ == "__main__":
    main()

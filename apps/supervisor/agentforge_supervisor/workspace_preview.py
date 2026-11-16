"""Fixed, offline document rendering program injected into the selected desktop."""


def open_document(raw, suffix, path):
    import base64
    import html
    import os
    import runpy
    import shutil
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    office = {".pptx", ".docx", ".xlsx"}
    images = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    text = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}
    if suffix not in office | images.keys() | text | {".pdf"}:
        raise ValueError("Unsupported preview type")
    if suffix in office and not shutil.which("libreoffice"):
        return dict(
            path=path,
            pages=0,
            truncated=False,
            previewed=False,
            visual_reviewed=False,
            error="workspace_preview_application_missing",
        )
    truncated = False
    with tempfile.TemporaryDirectory(prefix="af-preview-") as temporary:
        root = Path(temporary)
        source = root / ("document" + suffix)
        source.write_bytes(raw)
        if suffix in office:
            profile = root / "profile"
            (profile / "user").mkdir(parents=True)
            (profile / "user/registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                "</item></oor:items>"
            )
            subprocess.run(
                [
                    "libreoffice",
                    "-env:UserInstallation=" + profile.as_uri(),
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(root),
                    str(source),
                ],
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            source = root / "document.pdf"
            suffix = ".pdf"
        if suffix == ".pdf":
            if not shutil.which("pdftoppm"):
                return dict(
                    path=path,
                    pages=0,
                    truncated=False,
                    previewed=False,
                    visual_reviewed=False,
                    error="workspace_preview_application_missing",
                )
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    "10",
                    "-scale-to",
                    "1200",
                    "-png",
                    str(source),
                    str(root / "page"),
                ],
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pages = sorted(root.glob("page-*.png"))
            if not pages or sum(p.stat().st_size for p in pages) > 25 * 1024 * 1024:
                raise ValueError("Preview exceeds budget")
            body = "".join(
                '<img src="data:image/png;base64,'
                + base64.b64encode(p.read_bytes()).decode()
                + '">'
                for p in pages
            )
            count = len(pages)
            truncated = count == 10  # Conservative: may have more pages; do not claim completeness.
        elif suffix in images:
            body = (
                '<img src="data:'
                + images[suffix]
                + ";base64,"
                + base64.b64encode(raw).decode()
                + '">'
            )
            count = 1
        else:
            value = raw.decode("utf-8")
            body = "<pre>" + html.escape(value[:20000]) + "</pre>"
            count, truncated = 1, len(value) > 20000
        document = (
            '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">"
            "<title>AgentForge file preview</title><style>body{background:#eee;padding:24px}"
            "img{display:block;max-width:100%;margin:12px auto}pre{white-space:pre-wrap}</style>"
            "<h2>" + html.escape(path) + "</h2>" + body
        )
        # Same image-bundled CDP client as the existing visible browser adapter.
        driver = runpy.run_path("/usr/local/bin/rakazo-page-browser")
        os.environ["DISPLAY"] = ":1"
        os.environ["HOME"] = "/tmp/home"
        port = driver["cdp_port"]()
        try:
            driver["wait_for_cdp"](port, timeout_s=0.5)
        except RuntimeError:
            subprocess.Popen(
                ["/usr/local/bin/rakazo-browser", "about:blank"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            driver["wait_for_cdp"](port, timeout_s=15)
        cdp = driver["CdpClient"](driver["discover_ws_url"](port))
        cdp.connect()
        context = None
        try:
            previews = cdp.call("Target.getTargets")["targetInfos"]
            if sum(t.get("title") == "AgentForge file preview" for t in previews) >= 6:
                return dict(
                    path=path,
                    pages=0,
                    truncated=False,
                    previewed=False,
                    visual_reviewed=False,
                    error="workspace_preview_limit",
                )
            context = cdp.call("Target.createBrowserContext")["browserContextId"]
            target = cdp.call(
                "Target.createTarget", {"url": "about:blank", "browserContextId": context}
            )["targetId"]
            session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]

            def call(method, params=None):
                return cdp.call(method, params, session_id=session, timeout_s=10)

            call("Page.enable")
            call("Emulation.setScriptExecutionDisabled", {"value": True})
            call("Page.setDownloadBehavior", {"behavior": "deny"})
            frame = call("Page.getFrameTree")["frameTree"]["frame"]["id"]
            call("Page.setDocumentContent", {"frameId": frame, "html": document})
            call("Page.bringToFront")
            for _ in range(20):
                ready = call(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "Array.from(document.images).every("
                            "i => i.complete && i.naturalWidth > 0)"
                        ),
                        "returnByValue": True,
                    },
                )
                if ready.get("result", {}).get("value") is True:
                    break
                time.sleep(0.25)
            else:
                raise ValueError("Preview images did not load")
            call("Page.captureScreenshot", {"format": "png"})
        except BaseException:
            if context:
                try:
                    cdp.call("Target.disposeBrowserContext", {"browserContextId": context})
                except Exception:
                    pass
            raise
        finally:
            cdp.close()
    return dict(path=path, pages=count, truncated=truncated, previewed=True, visual_reviewed=False)

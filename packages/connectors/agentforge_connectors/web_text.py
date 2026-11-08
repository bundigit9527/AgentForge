"""Small bounded HTML reader; no scripts, resources, login state or subrequests."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree

from agentforge_core.browser import public_https_url
from agentforge_core.models import RunErrorCode
from agentforge_runtime.errors import RuntimeFailure

SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def normalize(value):
    return re.sub(r"\s+", " ", value).strip()


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        if self.tag in SKIP or "hidden" in self.attrs or self.attrs.get("aria-hidden") == "true":
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)

    def walk(self):
        yield self
        if self.tag in SKIP:
            return
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def has_class(self, name):
        return name in (self.attrs.get("class") or "").split()


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.count = 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 30000 or len(self.stack) > 100:
            raise RuntimeFailure(RunErrorCode.WEB_LIMIT_EXCEEDED)
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def readable(html):
    document = Document(html)
    nodes = list(document.root.walk())
    title = next((normalize(n.text())[:240] for n in nodes if n.tag == "title"), "")
    main = next((n for n in nodes if n.tag in {"article", "main"}), document.root)
    return title, normalize(main.text())


def duckduckgo_results(html, search_url, allowed, limit):
    nodes = list(Document(html).root.walk())
    results = []
    seen = set()
    for node in nodes:
        if not node.has_class("result"):
            continue
        descendants = list(node.walk())
        link = next((n for n in descendants if n.tag == "a" and n.has_class("result__a")), None)
        if link is None:
            continue
        try:
            raw = urljoin(search_url, link.attrs.get("href") or "")
            raw = parse_qs(urlsplit(raw).query).get("uddg", [raw])[0]
            public_https_url(raw)
            if len(raw) > 2048:
                continue
            if not allowed(raw) or raw in seen:
                continue
        except ValueError:
            continue
        title = normalize(link.text())[:240]
        snippet = next(
            (normalize(n.text())[:600] for n in descendants if n.has_class("result__snippet")), ""
        )
        if title:
            candidate = {"url": raw, "title": title, "snippet": snippet}
            if len(json.dumps([*results, candidate], ensure_ascii=False)) > 15000:
                break
            results.append(candidate)
            seen.add(raw)
        # Keep serialized results well below the shared ToolExecutor 20k budget.
        if len(results) >= limit or sum(len(str(r)) for r in results) > 13000:
            break
    if not results and not any(n.has_class("no-results") for n in nodes):
        # CAPTCHA / layout changes must not masquerade as a valid empty result.
        raise RuntimeFailure(RunErrorCode.WEB_UNAVAILABLE)
    return results


def bing_results(xml, allowed, limit):
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED) from None
    if root.tag != "rss" or root.find("channel") is None:
        raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
    results, seen = [], set()
    for item in root.findall("./channel/item"):
        url = (item.findtext("link") or "").strip()
        try:
            public_https_url(url)
            if len(url) > 2048 or url in seen or not allowed(url):
                continue
        except ValueError:
            continue
        title = normalize(item.findtext("title") or "")[:240]
        snippet = normalize(Document(item.findtext("description") or "").root.text())[:600]
        if not title:
            continue
        candidate = {"title": title, "url": url, "snippet": snippet}
        if len(json.dumps([*results, candidate], ensure_ascii=False)) > 15000:
            break
        results.append(candidate)
        seen.add(url)
        if len(results) >= limit:
            break
    return results

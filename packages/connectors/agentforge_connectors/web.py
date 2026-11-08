"""Anonymous web reads in the Python Worker, independent of Computer sessions."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from agentforge_core.browser import permitted_url, public_https_url
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure

from agentforge_connectors.browser import BrowserPolicy
from agentforge_connectors.public_http import fetch_public
from agentforge_connectors.web_text import bing_results, duckduckgo_results, readable

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
SEARCH_PROVIDERS = {
    "duckduckgo": SEARCH_ENDPOINT,
    "bing_rss": "https://www.bing.com/search?format=rss",
}


def search_endpoint():
    provider = os.environ.get("AGENTFORGE_WEB_SEARCH_PROVIDER", "duckduckgo")
    if provider not in SEARCH_PROVIDERS:
        raise ValueError("Unsupported web search provider")
    return SEARCH_PROVIDERS[provider]


def policy():
    filename = os.environ.get("AGENTFORGE_WEB_POLICY_FILE")
    if not filename:
        return BrowserPolicy.from_env()
    try:
        with Path(filename).open("rb") as stream:
            data = stream.read(128001)
        if len(data) > 128000:
            raise ValueError()
        return BrowserPolicy.model_validate_json(data)
    except (ValueError, OSError):
        raise ValueError("Invalid web policy file") from None


def enabled():
    value = os.environ.get("AGENTFORGE_WEB_ENABLED", "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("AGENTFORGE_WEB_ENABLED must be true or false")
    return value == "true"


def prepare_web(scope, snapshot):
    origins = (
        policy().origins(scope)
        if enabled() and snapshot.model.provider == "chat_completions"
        else ()
    )
    return ExecutionSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "web_origins": origins,
            "web_search_endpoint": search_endpoint()
            if origins and permitted_url(search_endpoint(), origins)
            else None,
        }
    )


def timestamp():
    return datetime.now(UTC).isoformat()


class WebService:
    def __init__(self, scope, snapshot, checkpoint, *, fetcher=fetch_public):
        self.scope, self.snapshot, self.checkpoint, self.fetcher = (
            scope,
            snapshot,
            checkpoint,
            fetcher,
        )

    def result_limit(self):
        # Leave room for tool schemas, the question, and several sources. Count real
        # UTF-8 bytes: Chinese page text is often three times its character count.
        budget = self.snapshot.model.context_max_bytes
        return min(18000, max(1024, budget // 8)) if budget else 18000

    def result_size(self, result):
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def guard(self, url):
        if not self.checkpoint():
            raise ExecutionStopped()
        try:
            public_https_url(url)
            allowed = (
                enabled()
                and permitted_url(url, self.snapshot.web_origins)
                and permitted_url(url, policy().origins(self.scope))
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise RuntimeFailure(RunErrorCode.WEB_NOT_ALLOWED)

    def allowed_result(self, url):
        # Ignore unapproved search hits without fetching them or broadening the grant.
        try:
            self.guard(url)
            return True
        except RuntimeFailure as exc:
            if exc.code == RunErrorCode.WEB_NOT_ALLOWED:
                return False
            raise

    def search(self, arguments):
        endpoint = search_endpoint()
        if self.snapshot.web_search_endpoint != endpoint:
            raise RuntimeFailure(RunErrorCode.WEB_NOT_ALLOWED)
        url = endpoint + ("&" if "?" in endpoint else "?") + urlencode({"q": arguments.query})
        self.guard(url)
        final, body, mime = self.fetcher(url, self.guard)
        self.guard(final)
        if endpoint == SEARCH_ENDPOINT:
            if mime not in {"text/html", "application/xhtml+xml"}:
                raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
            hits = duckduckgo_results(body, final, self.allowed_result, arguments.maxResults)
        else:
            if mime not in {"text/xml", "application/xml", "application/rss+xml"}:
                raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
            hits = bing_results(body, self.allowed_result, arguments.maxResults)
        self.guard(url)
        original_count = len(hits)
        result = {
            "query": arguments.query,
            "results": hits,
            "retrievedAt": timestamp(),
            "truncated": False,
        }
        while self.result_size(result) > self.result_limit() and hits:
            hits.pop()
            result["truncated"] = True
        if self.result_size(result) > self.result_limit() or (original_count and not hits):
            raise RuntimeFailure(RunErrorCode.WEB_LIMIT_EXCEEDED)
        return result

    def fetch(self, arguments):
        self.guard(arguments.url)
        url, body, mime = self.fetcher(arguments.url, self.guard)
        self.guard(url)
        title, text = (
            readable(body) if mime in {"text/html", "application/xhtml+xml"} else ("", body.strip())
        )
        if not text:
            raise RuntimeFailure(RunErrorCode.WEB_CONTENT_UNSUPPORTED)
        self.guard(url)
        result = {
            "url": url,
            "title": title,
            "text": text[: arguments.maxChars],
            "truncated": len(text) > arguments.maxChars,
            "retrievedAt": timestamp(),
        }
        while self.result_size(result) > self.result_limit() and result["text"]:
            result["text"] = result["text"][: int(len(result["text"]) * 0.8)]
            result["truncated"] = True
        if self.result_size(result) > self.result_limit():
            raise RuntimeFailure(RunErrorCode.WEB_LIMIT_EXCEEDED)
        return result

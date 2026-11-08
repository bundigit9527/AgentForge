"""Explicit public-network smoke check; does not use a database, key or Computer."""

import json
from urllib.parse import urlencode

from agentforge_connectors.public_http import fetch_public
from agentforge_connectors.web import SEARCH_ENDPOINT, search_endpoint
from agentforge_connectors.web_text import bing_results, duckduckgo_results, readable
from agentforge_core.browser import public_https_url
from agentforge_runtime.errors import RuntimeFailure


def main():
    query = "Python official documentation"
    endpoint = search_endpoint()
    for url in [
        endpoint + ("&" if "?" in endpoint else "?") + urlencode({"q": query}),
        "https://www.python.org/",
    ]:
        try:
            final, text, mime = fetch_public(url, public_https_url)
            result = (
                (
                    duckduckgo_results(text, final, lambda _: True, 3)
                    if endpoint == SEARCH_ENDPOINT
                    else bing_results(text, lambda _: True, 3)
                )
                if url.startswith(endpoint)
                else readable(text)[0]
            )
            print(json.dumps({"url": final, "result": result, "mime": mime}, ensure_ascii=False))
        except RuntimeFailure as exc:
            print(json.dumps({"url": url, "error": exc.code.value}))
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()

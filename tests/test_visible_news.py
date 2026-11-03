from uuid import uuid4

import anyio
import httpx
import pytest
from agentforge_core.mcp import McpFailure
from agentforge_runtime.mcp_tools import definition
from agentforge_supervisor.visible_news import HOME, document

from tests.test_mcp import Remote, make_service


def test_other_bot_cannot_activate_a_bound_screen(scope):
    bot = uuid4()
    remote = Remote()
    service = make_service(scope, remote, bot_id=bot)
    tool = service.catalog(scope, "products")[0]
    with pytest.raises(McpFailure):
        definition(service, scope, tool, lambda: True, str(uuid4()))
    assert not remote.calls()
    assert definition(service, scope, tool, lambda: True, str(bot)).effect == "read"


@pytest.mark.parametrize(
    ("url", "method", "index"),
    [
        ("https://127.0.0.1/private", "GET", 0),
        (HOME, "POST", 0),
        ("https://www.chinanews.com.cn.evil.test/cj/2026/11-03/1.shtml", "GET", 1),
        (HOME + "?token=untrusted", "GET", 0),
    ],
)
def test_broker_rejects_unapproved_request_before_network(url, method, index):
    def unreachable(_):
        pytest.fail("Unapproved request reached the network")

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
            with pytest.raises(ValueError):
                await document(client, {"url": url, "method": method}, index)

    anyio.run(check)

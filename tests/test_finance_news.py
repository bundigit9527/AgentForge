from datetime import datetime

import pytest
from agentforge_supervisor.finance_news import SHANGHAI, todays_headlines

NOW = datetime(2026, 11, 3, 12, tzinfo=SHANGHAI)


def test_feed_filters_and_orders_by_beijing_publication_date():
    items = [
        ("older", "Mon, 2 Nov 2026 15:59:00 +0000", "old"),
        ("midnight", "Mon, 2 Nov 2026 16:00:00 +0000", "one"),
        ("LATEST", "Tue, 3 Nov 2026 11:00:00 +0800", "two"),
        ("duplicate", "Tue, 3 Nov 2026 10:00:00 +0800", "two"),
        ("future", "Tue, 3 Nov 2026 15:00:00 +0800", "later"),
    ]
    entries = "".join(
        f"<item><title>{title}</title><pubDate>{when}</pubDate><link>https://www.chinanews.com.cn/cj/{link}.shtml</link></item>"
        for title, when, link in items
    )
    data = f"<rss><channel><title>财经</title>{entries}</channel></rss>".encode()
    result = todays_headlines(data, NOW, "")
    assert result.index("11:00 | LATEST") < result.index("00:00 | midnight")
    assert result.count(".shtml") == 2
    assert "midnight" not in todays_headlines(data, NOW, "latest")
    assert "未找到" in todays_headlines(data, NOW, "xyz")


def test_invalid_response_is_not_reported_as_no_news():
    with pytest.raises(ValueError):
        todays_headlines(b"<html/>", NOW, "")

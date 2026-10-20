from datetime import datetime

import pytest

from attention_budget.models import Item


def make_item(id: str, channel: str = "search", platform: str | None = None, **kw) -> Item:
    platform = platform or id.split(":", 1)[0]
    base = dict(id=id, platform=platform, channel=channel, author=kw.pop("author", "作者"),
                title=kw.pop("title", f"标题 {id}"), snippet=kw.pop("snippet", "正文"),
                url=kw.pop("url", f"https://example.com/{id}"), published_at=kw.pop("published_at", None))
    base.update(kw)
    return Item(**base)


@pytest.fixture
def mk():
    return make_item


NOW = datetime(2026, 10, 21, 8, 0)

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


class Env:
    def __init__(self, tmp_path):
        from datetime import date

        from attention_budget.config import Config
        from attention_budget.guard import Guard
        from attention_budget.ledger import Ledger

        self.cfg = Config.model_validate({"deepseek": {"api_key": "k"},
                                          "telegram": {"bot_token": "t", "chat_id": 42},
                                          "data_dir": str(tmp_path / "data")})
        self.ledger = Ledger(tmp_path / "l.db")
        self.run, _ = self.ledger.start_run(date(2026, 10, 21))
        self.guard = Guard(self.cfg.caps, self.cfg.budget, self.ledger, self.run)
        self.tmp = tmp_path

    def ledger_with_deep_picks(self, n, channel="search"):
        items = [make_item(f"xhs:{i}", channel) for i in range(n)]
        self.ledger.save_items(self.run, items)
        for it in items:
            self.ledger.add_verdict(self.run, it.id, "deep", None, "pick", "好", note_to_user="给你")
        return self.ledger


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)

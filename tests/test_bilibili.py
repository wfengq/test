import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from attention_budget.collectors.bilibili import AuthExpired, BiliCollector
from attention_budget.models import SourceFailure

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeBiliApi:
    def __init__(self, code=0, no_subtitle=False, valid=True):
        self.code, self.no_subtitle, self.valid = code, no_subtitle, valid

    def _guard(self):
        if self.code == -101:
            raise AuthExpired("账号未登录")

    def check_valid(self, cred):
        return self.valid

    def dynamic_page(self, cred, offset):
        self._guard()
        return load("bili_dynamic.json")

    def homepage(self, cred):
        self._guard()
        return load("bili_homepage.json")

    def search(self, keyword, page):
        self._guard()
        return load("bili_search.json")

    def video_info(self, bvid, cred):
        return {"cid": 7, "aid": 8}

    def subtitle_info(self, bvid, cid, cred):
        if self.no_subtitle:
            return {"subtitles": []}
        return {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/x.json"}]}

    def fetch_json(self, url):
        assert url.startswith("https://")
        return {"body": [{"content": "第一句"}, {"content": "第二句"}]}

    def comments(self, aid, cred):
        return {"replies": [{"content": {"message": "讲得好"}, "member": {"uname": "观众"}, "like": 9}]}

    def user_videos(self, mid, n, cred):
        return {"list": {"vlist": [{"title": "近作一", "bvid": "BV1a"}, {"title": "近作二", "bvid": "BV1b"}]}}


def tmp_cred(tmp_path):
    p = tmp_path / "bili_credential.json"
    p.write_text(json.dumps({"sessdata": "s", "bili_jct": "j", "dedeuserid": "1"}), encoding="utf-8")
    return p


def test_search_strips_highlight_tags_and_marks_query(tmp_path):
    items = BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi()).search("DeepSeek", 20)
    assert items[0].title == "DeepSeek 新功能实测" and items[0].channel == "search"
    assert items[0].query == "DeepSeek" and items[0].id == "bili:BV1srch"
    assert items[0].url == "https://www.bilibili.com/video/BV1srch" and items[0].author_id == "31"


def test_follow_updates_respects_since(tmp_path):
    since = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
    items = BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi()).follow_updates(since, 100)
    assert [i.id for i in items] == ["bili:BV1new"]
    assert items[0].channel == "follow" and items[0].metrics["play"] == 12000


def test_recommend_skips_empty_cards(tmp_path):
    items = BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi()).recommend(50)
    assert [i.id for i in items] == ["bili:BV1rec"] and items[0].channel == "recommend"


def test_expired_credential_raises_source_failure(tmp_path):
    with pytest.raises(SourceFailure, match="登录过期"):
        BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi(code=-101)).recommend(50)


def test_missing_credential_file_raises_source_failure(tmp_path):
    with pytest.raises(SourceFailure, match="登录过期"):
        BiliCollector(tmp_path / "none.json", api=FakeBiliApi()).recommend(50)


def test_subtitle_missing_returns_marker(tmp_path):
    assert BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi(no_subtitle=True)).tools()["subtitle"]("BV1") == "无字幕"


def test_subtitle_text_joined(tmp_path):
    assert BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi()).tools()["subtitle"]("BV1") == "第一句\n第二句"


def test_comments_and_up_recent(tmp_path):
    t = BiliCollector(tmp_cred(tmp_path), api=FakeBiliApi()).tools()
    assert "讲得好" in t["comments"]("BV1") and "近作二" in t["up_recent"]("11")

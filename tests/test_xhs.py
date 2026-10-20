import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from attention_budget.collectors.xhs import XhsCollector, XhsProcess, parse_feeds, parse_profile_link
from attention_budget.config import XhsCfg
from attention_budget.models import SourceFailure
from tests.conftest import make_item

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class MockXhs:
    def __init__(self, logged_in=True):
        self.sent: list[dict] = []
        self.logged_in = logged_in

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        if path != "/api/v1/login/status":
            self.sent.append({"method": request.method, "path": path, "json": body,
                              "auth": request.headers.get("authorization")})
        routes = {
            "/api/v1/login/status": {"success": True, "data": {"is_logged_in": self.logged_in}},
            "/api/v1/feeds/list": load("xhs_feeds.json"),
            "/api/v1/feeds/search": load("xhs_search.json"),
            "/api/v1/feeds/detail": load("xhs_detail.json"),
            "/api/v1/user/profile": load("xhs_profile.json"),
        }
        if path not in routes:
            return httpx.Response(404, json={"success": False})
        return httpx.Response(200, json=routes[path])

    def collector(self, sleep=lambda s: None, rand=lambda a, b: a, ocr=None, **cfg):
        proc = SimpleNamespace(base_url="http://127.0.0.1:18060", token="secret")
        client = httpx.Client(transport=httpx.MockTransport(self.handler), base_url=proc.base_url)
        kw = {"ocr": ocr} if ocr else {"ocr": lambda urls, n: ""}
        return XhsCollector(proc, XhsCfg(**cfg), sleep=sleep, rand=rand, client=client, **kw)


@pytest.fixture
def mock_xhs():
    return MockXhs()


def test_non_whitelisted_endpoint_never_sent(mock_xhs):
    xc = mock_xhs.collector()
    with pytest.raises(PermissionError):
        xc._request("POST", "/api/v1/feeds/comment", json={"feed_id": "1", "xsec_token": "t", "content": "hi"})
    assert mock_xhs.sent == []


def test_parse_feeds_keeps_notes_only_and_builds_url():
    items = parse_feeds(load("xhs_feeds.json"), "recommend")
    assert len(items) == 3 and items[0].id == "xhs:64f0000000000000000000a1"
    assert "xsec_token=tok1" in items[0].url and items[0].metrics["likes"] == 12000
    assert items[1].author == "博主乙" and items[0].xsec_token == "tok1" and items[0].author_id == "u1"


def test_followed_search_sends_scope_and_marks_follow(mock_xhs):
    items = mock_xhs.collector().search("Agent 记忆", 20, scope="followed")
    body = mock_xhs.sent[-1]["json"]
    assert body["filters"] == {"search_scope": "已关注", "publish_time": "一周内"}
    assert all(i.channel == "follow" for i in items) and items[0].query == "Agent 记忆"


def test_all_search_marks_search(mock_xhs):
    items = mock_xhs.collector().search("q", 20, scope="all")
    assert mock_xhs.sent[-1]["json"] == {"keyword": "q"} and items[0].channel == "search"


def test_sends_bearer_token(mock_xhs):
    mock_xhs.collector().recommend(50)
    assert mock_xhs.sent[0]["auth"] == "Bearer secret"


def test_waits_between_requests(mock_xhs):
    waits = []
    xc = mock_xhs.collector(sleep=waits.append, rand=lambda a, b: b)
    xc.recommend(50)
    xc.search("q", 20, scope="all")
    assert waits == [8.0, 8.0]  # the first request (login check) does not wait


def test_detail_includes_ocr_and_respects_cap(mock_xhs):
    xc = mock_xhs.collector(detail_cap=1, ocr=lambda urls, n: f"OCR({len(urls)}张)")
    xc.remember([make_item("xhs:64f0000000000000000000a1", xsec_token="tok1")])
    first = xc.tools()["note_detail"]("xhs:64f0000000000000000000a1")
    assert "OCR(2张)" in first and "干货" in first and "今天讲三种记忆方案" in first
    assert mock_xhs.sent[-1]["json"] == {"feed_id": "64f0000000000000000000a1", "xsec_token": "tok1"}
    assert "额度已用完" in xc.tools()["note_detail"]("xhs:64f0000000000000000000a1")


def test_key_creators_updates_marks_follow(mock_xhs):
    link = "https://www.xiaohongshu.com/user/profile/kc1?xsec_token=ptok&xsec_source=pc_note"
    items = mock_xhs.collector().key_creators_updates([link], 10)
    assert [i.id for i in items] == ["xhs:64f0000000000000000000c1"] and items[0].channel == "follow"
    assert mock_xhs.sent[-1]["json"] == {"user_id": "kc1", "xsec_token": "ptok", "tab": "note"}


def test_logged_out_raises_source_failure():
    with pytest.raises(SourceFailure, match="未登录"):
        MockXhs(logged_in=False).collector().recommend(50)


def test_circuit_breaker_after_error():
    m = MockXhs()
    orig = m.handler
    m.handler = lambda r: httpx.Response(500, json={"success": False}) if r.url.path == "/api/v1/feeds/list" else orig(r)
    xc = m.collector()
    with pytest.raises(SourceFailure):
        xc.recommend(50)
    n = len(m.sent)
    with pytest.raises(SourceFailure):
        xc.recommend(50)
    assert len(m.sent) == n


def test_zero_items_is_source_failure():
    m = MockXhs()
    orig = m.handler
    m.handler = lambda r: (httpx.Response(200, json={"success": True, "data": {"feeds": [], "count": 0}})
                           if r.url.path == "/api/v1/feeds/list" else orig(r))
    with pytest.raises(SourceFailure, match="0 条"):
        m.collector().recommend(50)


def test_profile_link_without_token_rejected():
    with pytest.raises(ValueError, match="xsec_token"):
        parse_profile_link("https://www.xiaohongshu.com/user/profile/abc")


class FakeProc:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


class FakePopen:
    def __init__(self):
        self.last = None
        self.args = None

    def __call__(self, args, **kw):
        self.args = args
        self.last = FakeProc()
        return self.last


class NeverHealthy:
    @staticmethod
    def get(url, timeout=None):
        raise httpx.ConnectError("refused")


class Healthy:
    @staticmethod
    def get(url, timeout=None):
        return httpx.Response(200, json={"status": "ok"})


class FakeClock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_process_health_timeout_kills(tmp_path):
    exe = tmp_path / "xiaohongshu-mcp.exe"
    exe.write_bytes(b"")
    popen = FakePopen()
    with pytest.raises(SourceFailure, match="没起来"):
        with XhsProcess(exe, 18060, popen=popen, http=NeverHealthy(), clock=FakeClock(step=1), sleep=lambda s: None):
            pass
    assert popen.last.killed


def test_process_starts_headless_with_random_token_and_stops(tmp_path):
    exe = tmp_path / "xiaohongshu-mcp.exe"
    exe.write_bytes(b"")
    popen = FakePopen()
    with XhsProcess(exe, 18060, popen=popen, http=Healthy(), clock=FakeClock(step=1), sleep=lambda s: None) as p:
        assert "-headless=true" in popen.args and "-port=:18060" in popen.args
        assert f"-token={p.token}" in popen.args and len(p.token) >= 24
    assert popen.last.killed


def test_missing_exe_is_source_failure(tmp_path):
    with pytest.raises(SourceFailure, match="exe_path"):
        with XhsProcess(None, 18060):
            pass

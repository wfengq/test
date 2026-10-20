"""The dry-run failure of 2026-10-20: one cold-start timeout on login/status killed every Xiaohongshu request.

Real sequence (from the user's terminal output and xiaohongshu-mcp log):
  XhsProcess startup confirmed login/status fine → XhsCollector.logged_in() asked login/status *again*
  → xiaohongshu-mcp launched a new browser, overran its deadline → HTTP 500 "context deadline exceeded"
  → no retry on that path → the error was cached in _broken → recommend + 4 searches all re-raised it.
"""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from attention_budget.collectors.xhs import XhsCollector, explain_server_error
from attention_budget.config import XhsCfg
from attention_budget.models import SourceFailure

FIX = Path(__file__).parent / "fixtures"
DEADLINE = {"error": "服务器内部错误", "code": "INTERNAL_ERROR", "details": {}}


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Script:
    """Per-path queue of responses; the last one repeats. Records every request that reached the server."""

    def __init__(self, **queues):
        ok = {
            "/api/v1/login/status": [(200, {"success": True, "data": {"is_logged_in": True}})],
            "/api/v1/feeds/list": [(200, load("xhs_feeds.json"))],
            "/api/v1/feeds/search": [(200, load("xhs_search.json"))],
        }
        ok.update({k.replace("_", "/").replace("api/v1/", "/api/v1/", 1): v for k, v in queues.items()})
        self.q = ok
        self.hits: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits.append(path)
        queue = self.q[path]
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=body)


class Proc:
    """Stands in for a started XhsProcess, including its log for explain()."""

    def __init__(self, login_state=None, log=""):
        self.base_url, self.token, self.login_state, self.log = "http://127.0.0.1:18060", "t", login_state, log

    def explain(self, status, body, path=None):
        return explain_server_error(status, body, self.log, path=path)


def collector(script, proc=None, waits=None):
    proc = proc or Proc()
    client = httpx.Client(transport=httpx.MockTransport(script.handler), base_url=proc.base_url)
    return XhsCollector(proc, XhsCfg(), sleep=(waits.append if waits is not None else lambda s: None),
                        rand=lambda a, b: a, client=client, ocr=lambda u, n: "")


def log_line(path):
    return f'time="t" level=error msg="服务器内部错误: context deadline exceeded, path: {path}"\n'


# 1. no second login check when startup already confirmed it

def test_startup_login_state_is_reused_not_requested_again():
    s = Script()
    xc = collector(s, Proc(login_state=True))
    assert len(xc.recommend(50)) == 3
    assert "/api/v1/login/status" not in s.hits


def test_startup_logged_out_state_is_reused():
    s = Script()
    with pytest.raises(SourceFailure, match="未登录"):
        collector(s, Proc(login_state=False)).recommend(50)
    assert s.hits == []


def test_refresh_forces_a_real_login_check():
    s = Script(api_v1_login_status=[(200, {"success": True, "data": {"is_logged_in": False}})])
    xc = collector(s, Proc(login_state=True))
    assert xc.logged_in(refresh=True) is False and s.hits == ["/api/v1/login/status"]


# 2. the exact failure: a transient deadline on login/status must be retried on the collection path

def test_transient_login_status_timeout_is_retried_and_collection_continues():
    s = Script(api_v1_login_status=[(500, DEADLINE), (200, {"success": True, "data": {"is_logged_in": True}})])
    xc = collector(s, Proc(log=log_line("/api/v1/login/status")))
    assert len(xc.recommend(50)) == 3
    assert len(xc.search("q", 20, scope="all")) == 1
    assert s.hits.count("/api/v1/login/status") == 2


def test_transient_search_timeout_is_retried():
    s = Script(api_v1_feeds_search=[(500, DEADLINE), (200, load("xhs_search.json"))])
    xc = collector(s, Proc(login_state=True, log=log_line("/api/v1/feeds/search")))
    assert len(xc.search("q", 20, scope="all")) == 1 and s.hits.count("/api/v1/feeds/search") == 2


def test_retries_are_paced():
    waits = []
    s = Script(api_v1_feeds_list=[(500, DEADLINE), (200, load("xhs_feeds.json"))])
    collector(s, Proc(login_state=True, log=log_line("/api/v1/feeds/list")), waits).recommend(50)
    assert len(waits) == 1  # one pause before the retry (the first request of a collector never waits)


# 3. a transient failure must not poison the rest of the round

def test_exhausted_transient_failure_does_not_block_other_calls():
    s = Script(api_v1_feeds_search=[(500, DEADLINE), (500, DEADLINE), (500, DEADLINE), (200, load("xhs_search.json"))])
    xc = collector(s, Proc(login_state=True, log=log_line("/api/v1/feeds/search")))
    with pytest.raises(SourceFailure, match="deadline"):
        xc.search("q1", 20, scope="all")
    assert len(xc.search("q2", 20, scope="all")) == 1  # next search still reaches the server
    assert len(xc.recommend(50)) == 3


def test_endpoint_breaks_after_two_exhausted_calls():
    s = Script(api_v1_feeds_search=[(500, DEADLINE)])
    xc = collector(s, Proc(login_state=True, log=log_line("/api/v1/feeds/search")))
    for _ in range(2):
        with pytest.raises(SourceFailure):
            xc.search("q", 20, scope="all")
    hits = len(s.hits)
    with pytest.raises(SourceFailure):
        xc.search("q", 20, scope="all")
    assert len(s.hits) == hits  # stopped calling it, per spec §7


def test_permanent_error_is_not_retried_and_is_cached():
    s = Script(api_v1_feeds_list=[(500, {"error": "服务器内部错误"})])
    blocked = ('time="t" level=error msg="服务器内部错误: fork/exec C:\\\\x\\\\leakless.exe: Operation did not complete '
               'successfully because the file contains a virus or potentially unwanted software., path: /api/v1/feeds/list"\n')
    xc = collector(s, Proc(login_state=True, log=blocked))
    with pytest.raises(SourceFailure, match="安全中心"):
        xc.recommend(50)
    with pytest.raises(SourceFailure):
        xc.recommend(50)
    assert s.hits.count("/api/v1/feeds/list") == 1


def test_client_timeout_is_transient():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json=load("xhs_feeds.json"))

    proc = Proc(login_state=True)
    xc = XhsCollector(proc, XhsCfg(), sleep=lambda s: None, rand=lambda a, b: a, ocr=lambda u, n: "",
                      client=httpx.Client(transport=httpx.MockTransport(handler), base_url=proc.base_url))
    assert len(xc.recommend(50)) == 3 and calls["n"] == 2


# error attribution: a 500 on one path must not show another path's log line

def test_explain_uses_the_error_line_of_the_same_path():
    log = (log_line("/api/v1/login/status")
           + 'time="t" level=error msg="服务器内部错误: 没有找到搜索结果, path: /api/v1/feeds/search"\n'
           + 'time="t" level=error msg="POST /api/v1/feeds/search 500"\n'
           + log_line("/api/v1/feeds/list"))
    assert "没有找到搜索结果" in explain_server_error(500, None, log, path="/api/v1/feeds/search")


# end to end through run_daily with the real collector: the 2026-10-20 dry-run, replayed

def test_dry_run_of_2026_10_20_replayed_keeps_xiaohongshu(env):
    from tests.conftest import NOW, make_item
    from tests.fakes import FakeCollector, FakeLLM
    from attention_budget.pipeline import run_daily

    s = Script(api_v1_login_status=[(500, DEADLINE), (200, {"success": True, "data": {"is_logged_in": True}})])
    xhs = collector(s, Proc(log=log_line("/api/v1/login/status")))
    bili = FakeCollector("bili", {"follow_updates": [make_item("bili:BV1", "follow")]})
    llm = FakeLLM({
        "SearchPlan": [{"queries": [{"platform": "xhs", "scope": "all", "query": "q"},
                                    {"platform": "xhs", "scope": "followed", "query": "q2"}]}],
        "CoarseResult": [{"scores": [], "rescue": []}],
        "DeepStep": [{"action": "verdict", "decision": "reject", "reason": "x"}],
        "FinalResult": [{"picks": [], "serendipity": None, "reject_summary": []}],
    })
    r = run_daily(env.cfg, env.ledger, llm, bili, xhs, NOW)
    assert "xhs" not in r.source_status and r.status == "ok"
    platforms = {it.platform for it in env.ledger.run_items(r.run_id)}
    assert platforms == {"bili", "xhs"}

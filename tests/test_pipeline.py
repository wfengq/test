import pytest

from attention_budget.ledger import AlreadyRanToday
from attention_budget.llm import LLMError
from attention_budget.pipeline import run_daily, write_report
from tests.conftest import NOW, make_item
from tests.fakes import FakeCollector, FakeLLM

IDS = ["bili:BV1", "bili:BV2", "bili:BV3", "xhs:a", "xhs:b"]


class EnvFull:
    def __init__(self, env):
        self.cfg, self.ledger = env.cfg, env.ledger
        self.cfg.data_dir = env.tmp / "data"
        self.cfg.xhs.key_creators = ["https://www.xiaohongshu.com/user/profile/k?xsec_token=t"]
        self.bili = FakeCollector("bili", {"follow_updates": [make_item("bili:BV1", "follow")],
                                           "recommend": [make_item("bili:BV2", "recommend")],
                                           "search": [make_item("bili:BV3", "search")]})
        self.xhs = FakeCollector("xhs", {"recommend": [make_item("xhs:a", "recommend")],
                                         "key_creators_updates": [],
                                         "search": [make_item("xhs:b", "search")]})
        self.llm = FakeLLM({
            "SearchPlan": [{"queries": [{"platform": "bili", "query": "q1"},
                                        {"platform": "xhs", "scope": "all", "query": "q2"}]}],
            "CoarseResult": [{"scores": [{"id": i, "score": 50 + n, "reason": "r"} for n, i in enumerate(IDS)],
                              "rescue": []}],
            "DeepStep": [{"action": "verdict", "decision": "pick", "reason": "好", "note_to_user": "给你"}],
            "FinalResult": [{"picks": [{"id": "xhs:b", "rank": 1}, {"id": "bili:BV1", "rank": 2}],
                             "serendipity": {"id": "xhs:a", "reason": "换口味"},
                             "reject_summary": [{"category": "重复热点", "count": 2}]}],
        })

    def run(self, **kw):
        return run_daily(self.cfg, self.ledger, kw.pop("llm", self.llm), self.bili, self.xhs, NOW, **kw)


@pytest.fixture
def env_full(env):
    return EnvFull(env)


def test_full_offline_run_pushes_at_most_five(env_full):
    r = env_full.run()
    assert r.status == "ok" and 0 < len(r.picks) <= 5
    assert [p.item_id for _, p, _ in r.picks] == ["xhs:b", "bili:BV1", "xhs:a"]
    assert r.picks[2][1].serendipity and r.picks[0][2]["note_to_user"] == "给你"
    assert {i.id for i, _ in r.missed} == {"bili:BV2", "bili:BV3"}
    assert r.stats["seen"] == len(env_full.ledger.run_items(r.run_id)) == 5
    assert r.stats["reject_summary"] == [{"category": "重复热点", "count": 2}]
    assert set(env_full.xhs.remembered) >= {"xhs:a", "xhs:b"}


def test_xhs_failure_keeps_bili_running(env_full):
    env_full.xhs.fail_all("未登录或被挤下线")
    r = env_full.run()
    assert r.status == "partial" and "被挤下线" in r.source_status["xhs"]
    assert all(i.platform == "bili" for i, _, _ in r.picks)


def test_both_sources_failing_is_failed(env_full):
    env_full.xhs.fail_all("未登录")
    env_full.bili.fail_all("登录过期")
    r = env_full.run()
    assert r.status == "failed" and r.picks == []


def test_resume_skips_finished_stages(env_full):
    env_full.llm.fail_on("DeepStep", after=2)
    with pytest.raises(LLMError):
        env_full.run()
    coarse_calls = env_full.llm.count("CoarseResult")
    collect_calls = len(env_full.bili.calls)
    r = env_full.run(llm=env_full.llm.healed())
    assert env_full.llm.count("CoarseResult") == coarse_calls
    assert len(env_full.bili.calls) == collect_calls
    assert r.status == "ok" and len(r.picks) == 3


def test_second_run_same_day_does_not_repeat(env_full):
    env_full.run()
    with pytest.raises(AlreadyRanToday):
        env_full.run()


def test_cap_reached_still_finalizes(env_full):
    env_full.cfg.caps.tokens = 300
    r = env_full.run()
    assert r.capped and r.status in ("ok", "partial") and r.stats["deep_read"] < 5


def test_report_written(env_full):
    r = env_full.run()
    f = write_report(r, env_full.cfg.data_dir / "reports")
    text = f.read_text(encoding="utf-8")
    missed = text.split("值得看但没名额")[1].split("## 账单")[0]
    assert f.name == "2026-10-21.md" and "标题 bili:BV2" in missed and "重复热点 2 条" in text

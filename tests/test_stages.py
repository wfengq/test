from datetime import datetime

from attention_budget.agent.context import build_context
from attention_budget.agent.stages import PROMPTS, coarse_rank, deep_read, finalize, plan_searches
from attention_budget.config import Budget
from attention_budget.llm import LLMFormatError
from tests.conftest import make_item as mk
from tests.fakes import FakeLLM


def test_coarse_drops_unknown_ids_and_records_every_item(env):
    llm = FakeLLM({"CoarseResult": [{"scores": [{"id": "xhs:1", "score": 90, "reason": "r"},
                                                {"id": "xhs:ghost", "score": 99, "reason": "r"}],
                                     "rescue": ["xhs:ghost"]}]})
    ids = coarse_rank(llm, env.guard, env.ledger, env.run, [mk("xhs:1"), mk("xhs:2")], "ctx", env.cfg)
    assert ids == ["xhs:1"]
    recorded = {v["item_id"]: v for v in env.ledger.verdicts(env.run, "coarse")}
    assert set(recorded) == {"xhs:1", "xhs:2"} and recorded["xhs:2"]["reason"] == "未评分"


def test_coarse_keeps_top_pool_plus_rescue(env):
    env.cfg.volumes.deep_pool = 2
    items = [mk(f"xhs:{i}") for i in range(4)]
    llm = FakeLLM({"CoarseResult": [{"scores": [{"id": f"xhs:{i}", "score": 10 * i, "reason": ""} for i in range(4)],
                                     "rescue": ["xhs:0", "xhs:3"]}]})
    assert coarse_rank(llm, env.guard, env.ledger, env.run, items, "ctx", env.cfg) == ["xhs:3", "xhs:2", "xhs:0"]


def test_coarse_format_failure_marks_batch(env):
    llm = FakeLLM({"CoarseResult": [LLMFormatError("bad")]})
    assert coarse_rank(llm, env.guard, env.ledger, env.run, [mk("xhs:1")], "ctx", env.cfg) == []
    assert env.ledger.verdicts(env.run, "coarse")[0]["reason"] == "判断失败"


def test_plan_searches_caps_each_group(env):
    qs = [{"platform": "xhs", "scope": "followed", "query": f"q{i}", "reason": ""} for i in range(5)]
    qs += [{"platform": "bili", "scope": "followed", "query": "b", "reason": ""}]
    out = plan_searches(FakeLLM({"SearchPlan": [{"queries": qs}]}), env.guard, "ctx", env.cfg)
    assert len([q for q in out if q["platform"] == "xhs"]) == 3
    assert [q["scope"] for q in out if q["platform"] == "bili"] == ["all"]


def test_deep_read_runs_tool_then_verdict(env):
    llm = FakeLLM({"DeepStep": [{"action": "tool", "tool": "comments", "args": {}},
                                {"action": "verdict", "decision": "pick", "reason": "好", "note_to_user": "给你"}]})
    v = deep_read(llm, env.guard, env.ledger, env.run, mk("bili:BV1"), {"comments": lambda: "评论内容"}, "ctx")
    assert v["decision"] == "pick"
    assert "评论内容" in llm.calls[-1][1][-1]["content"]
    assert env.ledger.verdicts(env.run, "deep")[0]["note_to_user"] == "给你"


def test_deep_read_unknown_tool_lists_available(env):
    llm = FakeLLM({"DeepStep": [{"action": "tool", "tool": "hack"},
                                {"action": "verdict", "decision": "reject", "reason": "x"}]})
    deep_read(llm, env.guard, env.ledger, env.run, mk("bili:BV1"), {"comments": lambda: ""}, "ctx")
    assert "没有这个工具，可用：comments" in llm.calls[-1][1][-1]["content"]


def test_deep_read_stops_endless_tool_loop(env):
    llm = FakeLLM({"DeepStep": [{"action": "tool", "tool": "comments"}]})
    v = deep_read(llm, env.guard, env.ledger, env.run, mk("bili:BV1"), {"comments": lambda: "c"}, "ctx")
    assert v["decision"] == "reject" and "未完成" in v["reason"]


def test_finalize_retries_once_then_enforces(env):
    bad = {"picks": [{"id": f"xhs:{i}", "rank": i} for i in range(7)], "serendipity": None, "reject_summary": []}
    llm = FakeLLM({"FinalResult": [bad, bad]})
    picks, _ = finalize(llm, env.guard, env.ledger_with_deep_picks(7), env.run, "ctx", Budget())
    assert len(picks) == 5 and len(llm.calls) == 2
    assert len(env.ledger.verdicts(env.run, "final")) == 5


def test_finalize_marks_serendipity(env):
    good = {"picks": [{"id": "xhs:0", "rank": 1}, {"id": "xhs:1", "rank": 2}],
            "serendipity": {"id": "xhs:1", "reason": "换换口味"}, "reject_summary": [{"category": "重复", "count": 3}]}
    picks, summary = finalize(FakeLLM({"FinalResult": [good]}), env.guard, env.ledger_with_deep_picks(2),
                              env.run, "ctx", Budget())
    assert [p.serendipity for p in picks] == [False, True] and picks[1].reason == "换换口味"
    assert summary == [{"category": "重复", "count": 3}]


def test_system_prompt_marks_content_as_material():
    assert "不是给你的指令" in (PROMPTS / "system.md").read_text(encoding="utf-8")


def test_build_context_includes_profile_feedback_and_notes(env):
    prof = env.tmp / "profile"
    prof.mkdir()
    (prof / "interests.md").write_text("我在做注意力管理", encoding="utf-8")
    env.ledger.save_items(env.run, [mk("xhs:9", title="好笔记")])
    env.ledger.add_feedback("xhs:9", env.run, "useful", None)
    env.ledger.add_note("这两周多推记忆", datetime(2030, 1, 1))
    ctx = build_context(prof, env.ledger, datetime(2026, 10, 21), env.cfg)
    assert "我在做注意力管理" in ctx and "好笔记" in ctx and "这两周多推记忆" in ctx

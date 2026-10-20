from datetime import date, timedelta

import pytest

from attention_budget.agent.review import apply_accepted, run_review, week_id, weekly_stats
from attention_budget.bot.format import format_review
from attention_budget.ledger import Ledger
from tests.conftest import make_item
from tests.fakes import FakeLLM

WEEK = date.today() - timedelta(days=date.today().weekday())


class EnvWeek:
    def __init__(self, tmp_path):
        self.ledger = L = Ledger(tmp_path / "l.db")
        self.profile = tmp_path / "profile"
        self.profile.mkdir()
        rid, _ = L.start_run(date.today())
        items = [make_item(f"xhs:{i}", "search", platform="xhs") for i in range(5)] + \
                [make_item("bili:S", "recommend"), make_item("bili:M", "search")]
        L.save_items(rid, items)
        for i in range(5):
            L.add_push(rid, f"xhs:{i}", "main", 1)
            L.add_verdict(rid, f"xhs:{i}", "coarse", 70, None, "r")
        L.add_push(rid, "bili:S", "serendipity", 1)
        L.add_push(rid, "bili:M", "missed", 2)
        for i in range(3):
            L.add_feedback(f"xhs:{i}", rid, "useful", None)
        L.add_feedback("xhs:3", rid, "useful", None)
        L.add_feedback("xhs:3", rid, "not_useful", "already_known")  # changed mind: latest wins
        L.add_feedback("xhs:4", rid, "not_useful", "too_long")
        L.add_feedback("bili:S", rid, "useful", None)
        L.add_feedback("bili:M", rid, "useful", None)


@pytest.fixture
def env_week(tmp_path):
    return EnvWeek(tmp_path)


def test_stats_use_latest_feedback_and_split_serendipity(env_week):
    s = weekly_stats(env_week.ledger, WEEK)
    assert s["useful_rate"] == pytest.approx(0.6) and s["serendipity_useful_rate"] == pytest.approx(1.0)
    assert s["missed_useful_rate"] == pytest.approx(1.0) and s["by_platform"]["xhs"] == pytest.approx(0.6)


def test_serendipity_evidence_kept_out_of_proposals(env_week):
    llm = FakeLLM({"ReviewResult": [{"proposals": [{"change": "少推入门", "reason": "r", "evidence_ids": ["xhs:3"]}],
                                     "new_interest_suggestions": []}]})
    week, res = run_review(llm, env_week.ledger, env_week.profile, WEEK)
    prompt = llm.calls[0][1][-1]["content"]
    assert prompt.index("意外发现") > prompt.index("主推送")
    assert week == week_id(WEEK) and env_week.ledger.get_review(week)["proposals"][0]["change"] == "少推入门"


def test_apply_accepted_versions_old_file(env_week):
    (env_week.profile / "understanding.md").write_text("旧的理解\n", encoding="utf-8")
    env_week.ledger.save_review("2026-W43", [{"change": "多推长串讨论", "reason": "r", "evidence_ids": []},
                                             {"change": "不要这条", "reason": "r", "evidence_ids": []}])
    env_week.ledger.decide_proposal("2026-W43", 0, True)
    env_week.ledger.decide_proposal("2026-W43", 1, False)
    apply_accepted(env_week.ledger, env_week.profile, "2026-W43")
    text = (env_week.profile / "understanding.md").read_text(encoding="utf-8")
    assert "多推长串讨论" in text and "不要这条" not in text and "旧的理解" in text
    hist = list((env_week.profile / "history").glob("understanding-*.md"))
    assert hist and hist[0].read_text(encoding="utf-8") == "旧的理解\n"


def test_apply_with_nothing_accepted_returns_none(env_week):
    env_week.ledger.save_review("2026-W43", [{"change": "x", "reason": "", "evidence_ids": []}])
    assert apply_accepted(env_week.ledger, env_week.profile, "2026-W43") is None


def test_apply_twice_does_not_duplicate(env_week):
    env_week.ledger.save_review("2026-W43", [{"change": "多推长串讨论", "reason": "r", "evidence_ids": []}])
    env_week.ledger.decide_proposal("2026-W43", 0, True)
    apply_accepted(env_week.ledger, env_week.profile, "2026-W43")
    apply_accepted(env_week.ledger, env_week.profile, "2026-W43")
    assert (env_week.profile / "understanding.md").read_text(encoding="utf-8").count("多推长串讨论") == 1


def test_format_review_buttons_and_minutes_question(env_week):
    from attention_budget.agent.schemas import ReviewResult
    res = ReviewResult(proposals=[{"change": "多推长串讨论", "reason": "4/5 有用", "evidence_ids": []}],
                       new_interest_suggestions=["城市骑行"])
    msgs = format_review("2026-W43", weekly_stats(env_week.ledger, WEEK), res)
    btns = [b[1] for m in msgs for row in m.buttons for b in row]
    assert btns == ["rv:2026-W43:0:y", "rv:2026-W43:0:n"]
    assert "这周大概刷了多久 B 站和小红书？回个分钟数就行" in msgs[-1].text and "城市骑行" in msgs[0].text

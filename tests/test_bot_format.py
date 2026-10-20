from attention_budget.bot.format import Message, format_daily
from attention_budget.guard import FinalPick
from attention_budget.pipeline import RunResult
from tests.conftest import make_item


def result_with(picks=5, title=None, note="理由", source_status=None, missed=2, capped=False):
    items = [make_item(f"xhs:{i}", "search", title=title or f"标题{i}", url=f"https://www.xiaohongshu.com/explore/{i}")
             for i in range(picks + missed)]
    ps = [(items[i], FinalPick(items[i].id, i, i == picks - 1 and picks > 1, "换口味"),
           {"note_to_user": note, "no_source": i == 0}) for i in range(picks)]
    ms = [(items[picks + j], {"note_to_user": note}) for j in range(missed)]
    stats = {"seen": 300, "deep_read": 33, "reject_summary": [{"category": "重复热点", "count": 120}],
             "tokens_in": 1000, "tokens_out": 100, "cost": 0.12}
    return RunResult("r1", ps, ms, stats, source_status or {}, capped, "ok", day="2026-10-21",
                     extra={"report": "C:/reports/2026-10-21.md"})


def buttons(msgs):
    return [b for m in msgs for row in m.buttons for b in row]


def test_fewer_than_five_says_so_and_no_padding():
    msgs = format_daily(result_with(picks=2, missed=0))
    assert "今天只有 2 条值得看" in msgs[0].text
    assert len([b for b in buttons(msgs[:1]) if b[1].startswith("fb:")]) == 2 * 2


def test_long_content_split_without_cutting_items():
    r = result_with(picks=5, title="长" * 1500, note="理由" * 300)
    msgs = format_daily(r)
    assert all(len(m.text) <= 4096 for m in msgs) and len(msgs) > 2
    assert sum(m.text.count("https://") for m in msgs) == 5 + len(r.missed)


def test_note_capped_at_80_chars():
    msgs = format_daily(result_with(picks=1, note="字" * 200, missed=0))
    assert "字" * 80 in msgs[0].text and "字" * 81 not in msgs[0].text


def test_source_failure_banner():
    msgs = format_daily(result_with(picks=1, source_status={"xhs": "未登录或被挤下线"}))
    assert msgs[0].text.startswith("⚠️ 小红书：未登录或被挤下线，今天只看了 B站")


def test_serendipity_and_no_source_marks():
    text = format_daily(result_with(picks=3))[0].text
    assert "🎲 你没关注的：" in text and "（没读到原文）" in text


def test_bill_has_missed_stats_and_cap():
    msgs = format_daily(result_with(picks=1, missed=2, capped=True))
    bill = [m for m in msgs if "今日账单" in m.text][0]
    assert "今天看了 300 条，深读 33 条" in bill.text and "重复热点 120 条" in bill.text
    assert "触到上限" in bill.text and "C:/reports/2026-10-21.md" in bill.text
    assert {s for s in bill.slots} == {("xhs:1", "missed"), ("xhs:2", "missed")}


def test_slots_mark_main_and_serendipity():
    msgs = format_daily(result_with(picks=2, missed=0))
    assert msgs[0].slots == [("xhs:0", "main"), ("xhs:1", "serendipity")]

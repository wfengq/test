from datetime import date

import pytest

from attention_budget.config import Budget, Caps
from attention_budget.guard import CapReached, FinalPick, Guard, enforce_final, validate_final
from attention_budget.ledger import Ledger


def mk_guard(tmp_path, clock=None, **caps):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    kw = {"clock": clock} if clock else {}
    return Guard(Caps(**caps), Budget(), L, rid, **kw)


def test_unknown_or_unread_ids_are_dropped():
    picks = [FinalPick("xhs:ghost", 1, False, ""), FinalPick("xhs:1", 2, False, "")]
    out = enforce_final(picks, {"xhs:1"}, set(), {"xhs:1": "follow"}, Budget())
    assert [p.item_id for p in out] == ["xhs:1"]
    assert validate_final(picks, {"xhs:1"}, set(), {"xhs:1": "follow"}, Budget())


def test_never_pads_when_fewer_than_five():
    picks = [FinalPick(f"xhs:{i}", i, False, "") for i in range(2)]
    ids = {p.item_id for p in picks}
    assert len(enforce_final(picks, ids, set(), dict.fromkeys(ids, "search"), Budget())) == 2


def test_truncates_to_five_and_one_serendipity_not_from_follow():
    picks = [FinalPick(f"xhs:{i}", i, i in (0, 1, 2), "") for i in range(8)]
    ids = {p.item_id for p in picks}
    ch = dict.fromkeys(ids, "recommend")
    ch["xhs:0"] = "follow"
    out = enforce_final(picks, ids, set(), ch, Budget())
    assert len(out) == 5 and sum(p.serendipity for p in out) == 1
    assert all(not (p.serendipity and ch[p.item_id] == "follow") for p in out)
    assert validate_final(picks, ids, set(), ch, Budget())


def test_valid_final_has_no_problems():
    picks = [FinalPick("xhs:1", 1, False, ""), FinalPick("bili:BV1", 2, True, "r")]
    ch = {"xhs:1": "follow", "bili:BV1": "search"}
    assert validate_final(picks, set(ch), set(), ch, Budget()) == []


def test_already_pushed_is_dropped():
    out = enforce_final([FinalPick("xhs:1", 1, False, "")], {"xhs:1"}, {"xhs:1"}, {"xhs:1": "follow"}, Budget())
    assert out == []


def test_per_item_tool_cap_returns_message_without_calling(tmp_path):
    g = mk_guard(tmp_path)
    calls = []
    for _ in range(5):
        r = g.call_tool("xhs:1", "thread", lambda: calls.append(1) or "ok")
    assert len(calls) == 4 and "上限" in r


def test_total_tool_cap_raises(tmp_path):
    g = mk_guard(tmp_path, tool_calls=2)
    g.call_tool("a", "t", lambda: "ok")
    g.call_tool("b", "t", lambda: "ok")
    with pytest.raises(CapReached):
        g.call_tool("c", "t", lambda: "ok")


def test_tool_exception_becomes_message_and_is_logged(tmp_path):
    g = mk_guard(tmp_path)

    def boom():
        raise RuntimeError("网络断了")

    assert g.call_tool("a", "t", boom) == "工具失败：网络断了"
    assert g.ledger.usage(g.run_id)[2] == 1


def test_token_cap_exhausts(tmp_path):
    g = mk_guard(tmp_path, tokens=100)
    g.charge(60, 30)
    assert not g.exhausted
    g.charge(20, 0)
    assert g.exhausted
    with pytest.raises(CapReached):
        g.call_tool("a", "t", lambda: "ok")


def test_time_cap(tmp_path):
    t = [0.0]
    g = mk_guard(tmp_path, clock=lambda: t[0], minutes=40)
    t[0] = 40 * 60 + 1
    assert g.exhausted

from datetime import date, datetime, timedelta

import pytest

from attention_budget.ledger import AlreadyRanToday, Ledger


def test_same_day_second_start_raises_after_finish(tmp_path):
    L = Ledger(tmp_path / "l.db")
    rid, resumed = L.start_run(date(2026, 10, 21))
    assert not resumed
    L.finish_run(rid, "ok", {}, False)
    with pytest.raises(AlreadyRanToday):
        L.start_run(date(2026, 10, 21))


def test_force_starts_a_new_run_same_day(tmp_path):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    L.finish_run(rid, "ok", {}, False)
    rid2, resumed = L.start_run(date(2026, 10, 21), force=True)
    assert rid2 != rid and not resumed


def test_unfinished_run_is_resumed(tmp_path):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    L.set_stage(rid, "coarse")
    rid2, resumed = L.start_run(date(2026, 10, 21))
    assert rid2 == rid and resumed and L.get_stage(rid) == "coarse"


def test_merge_prefers_follow_channel(tmp_path, mk):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    new = L.save_items(rid, [mk("xhs:1", "recommend"), mk("xhs:1", "follow")])
    assert [i.channel for i in new] == ["follow"]
    assert L.save_items(rid, [mk("xhs:1", "search")]) == []
    assert [i.id for i in L.run_items(rid)] == ["xhs:1"]


def test_feedback_keeps_every_press_and_uses_latest(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.add_feedback("xhs:1", "r", "useful", None)
    L.add_feedback("xhs:1", "r", "not_useful", "too_long")
    useful, not_useful = L.latest_feedback(days=14, n_each=10)
    assert useful == [] and not_useful[0]["reason"] == "too_long"
    assert L.feedback_count("xhs:1") == 2


def test_usage_accumulates_with_tool_calls(tmp_path):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    L.add_usage(rid, 100, 20)
    L.add_usage(rid, 5, 1)
    L.add_tool_call(rid, "xhs:1", "note_detail", {}, True)
    assert L.usage(rid) == (105, 21, 1)


def test_notes_expire(tmp_path):
    L = Ledger(tmp_path / "l.db")
    now = datetime(2026, 10, 21, 9)
    L.add_note("多推 Agent 记忆", now + timedelta(days=3))
    assert L.active_notes(now) == ["多推 Agent 记忆"]
    assert L.active_notes(now + timedelta(days=4)) == []


def test_pushed_ids_and_verdicts(tmp_path):
    L = Ledger(tmp_path / "l.db")
    rid, _ = L.start_run(date(2026, 10, 21))
    L.add_verdict(rid, "bili:BV1", "coarse", 80, None, "不错")
    L.add_push(rid, "bili:BV1", "main", 7)
    assert L.pushed_ids() == {"bili:BV1"}
    assert L.verdicts(rid, "coarse")[0]["score"] == 80


def test_outbox_roundtrip(tmp_path):
    L = Ledger(tmp_path / "l.db")
    i = L.outbox_put({"text": "hi"})
    assert L.outbox_all() == [(i, {"text": "hi"})]
    L.outbox_delete(i)
    assert L.outbox_all() == []


def test_review_decisions_and_minutes(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.save_review("2026-W43", [{"change": "a"}, {"change": "b"}])
    L.decide_proposal("2026-W43", 1, True)
    assert L.get_review("2026-W43")["decisions"] == {"1": True}
    assert L.awaiting_minutes(datetime.now()) == "2026-W43"
    L.set_self_reported_minutes("2026-W43", 300)
    assert L.awaiting_minutes(datetime.now()) is None


def test_run_state_roundtrip(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.set_state("r", "shortlist", ["a", "b"])
    assert L.get_state("r", "shortlist") == ["a", "b"] and L.get_state("r", "nope", {}) == {}

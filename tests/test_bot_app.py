from datetime import datetime, timedelta

import httpx

from attention_budget.bot.app import handle_callback, handle_text, send_messages
from attention_budget.bot.format import Message
from attention_budget.ledger import Ledger


def test_ignores_other_chats(tmp_path):
    L = Ledger(tmp_path / "l.db")
    assert handle_callback(L, 42, 99, "fb:xhs:1:useful") is None and L.feedback_count("xhs:1") == 0


def test_useful_records_and_acknowledges(tmp_path):
    L = Ledger(tmp_path / "l.db")
    reply = handle_callback(L, 42, 42, "fb:xhs:1:useful")
    assert L.feedback_count("xhs:1") == 1 and "记下了" in reply.text


def test_not_useful_then_reason_records_both(tmp_path):
    L = Ledger(tmp_path / "l.db")
    reply = handle_callback(L, 42, 42, "fb:xhs:1:not_useful")
    assert [b[1] for row in reply.buttons for b in row][0] == "why:xhs:1:not_interested"
    handle_callback(L, 42, 42, "why:xhs:1:too_long")
    assert L.feedback_count("xhs:1") == 2 and L.latest_feedback(14, 10)[1][0]["reason"] == "too_long"


def test_review_button_decides(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.save_review("2026-W43", [{"change": "a"}])
    handle_callback(L, 42, 42, "rv:2026-W43:0:y")
    assert L.get_review("2026-W43")["decisions"] == {"0": True}


def test_note_with_two_weeks_expires_in_14_days(tmp_path):
    L = Ledger(tmp_path / "l.db")
    now = datetime(2026, 10, 21, 9)
    handle_text(L, 42, 42, "这两周多推 Agent 记忆", now)
    assert L.active_notes(now + timedelta(days=13)) and not L.active_notes(now + timedelta(days=15))


def test_note_with_n_days(tmp_path):
    L = Ledger(tmp_path / "l.db")
    now = datetime(2026, 10, 21, 9)
    handle_text(L, 42, 42, "3天内别推游戏", now)
    assert L.active_notes(now + timedelta(days=2)) and not L.active_notes(now + timedelta(days=4))


def test_number_after_review_is_minutes(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.save_review("2026-W43", [])
    reply = handle_text(L, 42, 42, "420", datetime.now())
    assert L.get_review("2026-W43")["self_reported_minutes"] == 420 and "420" in reply
    assert L.active_notes(datetime.now()) == []


def test_text_from_other_chat_ignored(tmp_path):
    L = Ledger(tmp_path / "l.db")
    assert handle_text(L, 42, 7, "hi", datetime.now()) is None and L.active_notes(datetime.now()) == []


def test_send_failure_goes_to_outbox_and_is_flushed_later(tmp_path):
    L = Ledger(tmp_path / "l.db")
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    send_messages("tok", 42, [Message("hello", [], [])], L, client=down)
    assert len(L.outbox_all()) == 1
    sent = []

    def ok(r):
        sent.append(r.read())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    up = httpx.Client(transport=httpx.MockTransport(ok))
    send_messages("tok", 42, [Message("second", [[("有用", "fb:xhs:1:useful")]], [("xhs:1", "main")])], L, run_id="r1", client=up)
    assert L.outbox_all() == [] and len(sent) == 2 and b"hello" in sent[0]
    assert L.pushed_ids() == {"xhs:1"}

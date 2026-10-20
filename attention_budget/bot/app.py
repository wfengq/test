"""Telegram side: send messages, take button presses and free-text notes."""
import re
from datetime import datetime, timedelta

import httpx

from ..ledger import Ledger
from .format import Message

REASONS = {"not_interested": "不感兴趣", "already_known": "早就知道了", "low_quality": "质量差", "too_long": "太长不值"}
API = "https://api.telegram.org/bot{token}/{method}"


def _parse_expiry(text: str, now: datetime, default_days: int) -> datetime:
    if m := re.search(r"(\d+)\s*天", text):
        return now + timedelta(days=int(m.group(1)))
    if m := re.search(r"(\d+)\s*周", text):
        return now + timedelta(weeks=int(m.group(1)))
    for word, days in (("这两周", 14), ("两周", 14), ("这周", 7), ("本周", 7), ("这个月", 30), ("本月", 30)):
        if word in text:
            return now + timedelta(days=days)
    return now + timedelta(days=default_days)


def handle_callback(ledger: Ledger, chat_id_cfg: int, chat_id: int, data: str) -> Message | None:
    if chat_id != chat_id_cfg:
        return None
    kind, _, rest = data.partition(":")
    if kind == "fb":
        item_id, value = rest.rsplit(":", 1)
        ledger.add_feedback(item_id, "", value, None)
        if value == "useful":
            return Message("👍 记下了")
        return Message("哪里不对？", [[(label, f"why:{item_id}:{key}")] for key, label in REASONS.items()])
    if kind == "why":
        item_id, reason = rest.rsplit(":", 1)
        ledger.add_feedback(item_id, "", "not_useful", reason)
        return Message(f"记下了：{REASONS.get(reason, reason)}")
    if kind == "rv":
        week, idx, yn = rest.rsplit(":", 2)
        ledger.decide_proposal(week, int(idx), yn == "y")
        return Message("✅ 采纳" if yn == "y" else "❌ 不采纳")
    return None


def handle_text(ledger: Ledger, chat_id_cfg: int, chat_id: int, text: str, now: datetime,
                default_days: int = 14) -> str | None:
    if chat_id != chat_id_cfg or not text.strip():
        return None
    week = ledger.awaiting_minutes(now)
    if week and text.strip().isdigit():
        ledger.set_self_reported_minutes(week, int(text.strip()))
        return f"记下了：这周大约 {int(text.strip())} 分钟"
    expires = _parse_expiry(text, now, default_days)
    ledger.add_note(text.strip(), expires)
    return f"记下了，{expires:%m月%d日}前都会参考这句话"


def _payload(chat_id: int, m: Message) -> dict:
    p = {"chat_id": chat_id, "text": m.text, "disable_web_page_preview": True}
    if m.buttons:
        p["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in m.buttons]}
    return p


def _post(client: httpx.Client, token: str, method: str, payload: dict) -> int:
    r = client.post(API.format(token=token, method=method), json=payload, timeout=30)
    body = r.json() if r.status_code == 200 else {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} 失败：{r.status_code}")
    return body["result"]["message_id"]


def send_messages(bot_token: str, chat_id: int, messages: list[Message], ledger: Ledger, run_id: str = "",
                  client: httpx.Client | None = None) -> list[int]:
    client = client or httpx.Client()
    for oid, payload in ledger.outbox_all():
        try:
            _post(client, bot_token, "sendMessage", payload)
            ledger.outbox_delete(oid)
        except Exception:
            break
    ids = []
    for m in messages:
        payload = _payload(chat_id, m)
        try:
            mid = _post(client, bot_token, "sendMessage", payload)
        except Exception:
            ledger.outbox_put(payload)
            mid = None
        ids.append(mid)
        for item_id, slot in m.slots:
            ledger.add_push(run_id, item_id, slot, mid)
    return ids


def send_photo(bot_token: str, chat_id: int, png: bytes, caption: str, client: httpx.Client | None = None) -> None:
    client = client or httpx.Client()
    client.post(API.format(token=bot_token, method="sendPhoto"), data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("qr.png", png, "image/png")}, timeout=30)


def run_bot(cfg, ledger: Ledger, on_review_done=None) -> None:
    """Long-poll Telegram and route button presses and text to the handlers above."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

    def markup(m: Message):
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                     for row in m.buttons]) if m.buttons else None

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        reply = handle_callback(ledger, cfg.telegram.chat_id, q.message.chat.id, q.data or "")
        if reply:
            await q.message.reply_text(reply.text, reply_markup=markup(reply))
        if on_review_done and (q.data or "").startswith("rv:"):
            on_review_done(q.data.split(":")[1])

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        reply = handle_text(ledger, cfg.telegram.chat_id, update.effective_chat.id, update.message.text or "",
                            datetime.now(), cfg.note_default_days)
        if reply:
            await update.message.reply_text(reply)

    app = Application.builder().token(cfg.telegram.bot_token).build()
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

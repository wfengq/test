"""Telegram copy: today's picks, the bill, the weekly review."""
from dataclasses import dataclass, field

from ..agent.stages import PLATFORM_TEXT
from ..pipeline import SOURCE_NAME, RunResult

LIMIT = 4096
NOTE_MAX = 80
TITLE_MAX = 1800  # keeps any single block well under one message


@dataclass
class Message:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    slots: list[tuple[str, str]] = field(default_factory=list)  # (item_id, main|serendipity|missed)


def _block(n: int, item, note: str, prefix: str = "", suffix: str = "") -> str:
    title = item.title[:TITLE_MAX] or "（无标题）"
    return f"{n}. [{PLATFORM_TEXT[item.platform]}] {prefix}{title}{suffix}\n{item.url}\n{note[:NOTE_MAX]}"


def _fb_row(n: int, item_id: str) -> list[tuple[str, str]]:
    return [(f"👍 有用 {n}", f"fb:{item_id}:useful"), (f"👎 没用 {n}", f"fb:{item_id}:not_useful")]


def _pack(header: str, blocks: list[tuple[str, list, tuple]], footer: str = "") -> list[Message]:
    """Split on item boundaries so no message exceeds Telegram's 4096 chars."""
    msgs, cur = [], Message(header)
    for text, row, slot in blocks:
        if len(cur.text) + 2 + len(text) > LIMIT and cur.slots:
            msgs.append(cur)
            cur = Message("（续）")
        cur.text += "\n\n" + text
        cur.buttons.append(row)
        cur.slots.append(slot)
    if footer:
        if len(cur.text) + 2 + len(footer) > LIMIT:
            msgs.append(cur)
            cur = Message(footer)
        else:
            cur.text += "\n\n" + footer
    msgs.append(cur)
    return msgs


def _banner(r: RunResult) -> str:
    if not r.source_status:
        return ""
    failed = [SOURCE_NAME.get(k, k) + "：" + v for k, v in r.source_status.items()]
    ok = [SOURCE_NAME[k] for k in SOURCE_NAME if k not in r.source_status]
    tail = f"，今天只看了 {'、'.join(ok)}" if ok else "，今天两个来源都没读到"
    return "⚠️ " + "；".join(failed) + tail + "\n\n"


def format_daily(r: RunResult) -> list[Message]:
    n = len(r.picks)
    title = f"今天的 {n} 条" if n >= 5 else f"今天只有 {n} 条值得看"
    header = _banner(r) + f"📌 {title}（{r.day}）"
    blocks = []
    for i, (item, pick, verdict) in enumerate(r.picks, start=1):
        prefix = "🎲 你没关注的：" if pick.serendipity else ""
        suffix = "（没读到原文）" if verdict.get("no_source") else ""
        note = (pick.reason + " · " if pick.serendipity and pick.reason else "") + verdict.get("note_to_user", "")
        blocks.append((_block(i, item, note, prefix, suffix), _fb_row(i, item.id),
                       (item.id, "serendipity" if pick.serendipity else "main")))
    main = _pack(header, blocks)

    s = r.stats
    lines = [f"今天看了 {s['seen']} 条，深读 {s['deep_read']} 条"]
    if s.get("reject_summary"):
        lines.append("主要筛掉：" + "，".join(f"{x['category']} {x['count']} 条" for x in s["reject_summary"]))
    if r.capped:
        lines.append("今天触到上限，深读提前结束")
    lines.append(f"花费约 ¥{s.get('cost', 0)}")
    if r.extra.get("report"):
        lines.append(f"完整报告：{r.extra['report']}")
    missed = [(_block(i, item, v.get("note_to_user", "")), _fb_row(i, item.id), (item.id, "missed"))
              for i, (item, v) in enumerate(r.missed, start=1)]
    bill_header = "🧾 今日账单" + (f"\n值得看但没名额（{len(missed)} 条）：" if missed else "")
    bill = _pack(bill_header, missed, "\n".join(lines))
    return main + bill


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0%}"


def format_review(week: str, stats: dict, result) -> list[Message]:
    head = (f"🗓 {week} 复盘\n推送 {stats['pushed']} 条，有用率 {_pct(stats['useful_rate'])}；"
            f"没名额里的有用率 {_pct(stats['missed_useful_rate'])}；意外发现有用率 {_pct(stats['serendipity_useful_rate'])}")
    if result.new_interest_suggestions:
        head += "\n\n要不要把这些方向加进你的兴趣？（直接回复告诉我）\n" + "\n".join(
            f"- {s}" for s in result.new_interest_suggestions)
    blocks = [(f"{i + 1}. {p.change}\n理由：{p.reason}", [("✅ 采纳", f"rv:{week}:{i}:y"), ("❌ 不采纳", f"rv:{week}:{i}:n")],
               (f"proposal:{i}", "review")) for i, p in enumerate(result.proposals)]
    if not blocks:
        head += "\n\n这周没有要改的地方。"
    msgs = _pack(head, blocks, "这周大概刷了多久 B 站和小红书？回个分钟数就行")
    for m in msgs:
        m.slots = []  # review messages are not item pushes
    return msgs

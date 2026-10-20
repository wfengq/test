"""What the AI knows about the person: their words, its own notes, their buttons."""
from datetime import datetime
from pathlib import Path

from ..ledger import Ledger

REASON_TEXT = {"not_interested": "不感兴趣", "already_known": "早就知道了", "low_quality": "质量差", "too_long": "太长不值"}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def build_context(profile_dir: Path, ledger: Ledger, now: datetime, cfg) -> str:
    parts = []
    if text := _read(Path(profile_dir) / "interests.md"):
        parts.append("## 使用者自己写的兴趣说明\n" + text)
    if text := _read(Path(profile_dir) / "understanding.md"):
        parts.append("## 你之前总结、并经使用者确认的「对他的理解」\n" + text)
    useful, not_useful = ledger.latest_feedback(days=14, n_each=10)
    if useful:
        parts.append("## 最近他标了「有用」的\n" + "\n".join(f"- [{f['platform']}] {f['title']}" for f in useful))
    if not_useful:
        parts.append("## 最近他标了「没用」的\n" + "\n".join(
            f"- [{f['platform']}] {f['title']}（{REASON_TEXT.get(f['reason'] or '', '没说原因')}）" for f in not_useful))
    if notes := ledger.active_notes(now):
        parts.append("## 他最近的留言（仍在有效期内）\n" + "\n".join(f"- {n}" for n in notes))
    return "\n\n".join(parts) or "（还没有任何关于使用者的信息，按'信息量高、有新东西'的通用标准判断）"

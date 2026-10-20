"""Weekly review: how the AI's calls compare with the person's buttons."""
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from ..ledger import Ledger
from .context import REASON_TEXT
from .schemas import ReviewResult
from .stages import PLATFORM_TEXT, _prompt, _system

SLOTS = ("main", "serendipity", "missed")


def week_id(week_start: date) -> str:
    y, w, _ = week_start.isocalendar()
    return f"{y}-W{w:02d}"


def _week_rows(ledger: Ledger, week_start: date) -> dict[str, list[dict]]:
    start = datetime.combine(week_start, datetime.min.time())
    end = start + timedelta(days=7)
    latest = ledger.latest_feedback_by_item()
    rows: dict[str, list[dict]] = {s: [] for s in SLOTS}
    seen = set()
    for p in ledger.pushes(start):
        if datetime.fromisoformat(p["at"]) >= end or (p["item_id"], p["slot"]) in seen:
            continue
        seen.add((p["item_id"], p["slot"]))
        item = ledger.get_item(p["item_id"])
        fb = latest.get(p["item_id"])
        rows[p["slot"]].append({"item_id": p["item_id"], "platform": item.platform if item else "",
                                "title": item.title if item else "", "run_id": p["run_id"],
                                "feedback": fb["value"] if fb else None, "reason": fb["reason"] if fb else None})
    return rows


def _rate(rows: list[dict]) -> float | None:
    rated = [r for r in rows if r["feedback"]]
    return sum(r["feedback"] == "useful" for r in rated) / len(rated) if rated else None


def weekly_stats(ledger: Ledger, week_start: date) -> dict:
    rows = _week_rows(ledger, week_start)
    by_platform = {}
    for plat in ("bili", "xhs"):
        by_platform[plat] = _rate([r for r in rows["main"] if r["platform"] == plat])
    return {"useful_rate": _rate(rows["main"]), "missed_useful_rate": _rate(rows["missed"]),
            "serendipity_useful_rate": _rate(rows["serendipity"]), "by_platform": by_platform,
            "pushed": len(rows["main"]) + len(rows["serendipity"]),
            "rated": sum(1 for s in SLOTS for r in rows[s] if r["feedback"])}


def _fmt_rows(ledger: Ledger, rows: list[dict]) -> str:
    out = []
    for r in rows:
        scores = [v["score"] for v in ledger.verdicts(r["run_id"], "coarse") if v["item_id"] == r["item_id"]]
        fb = {"useful": "有用", "not_useful": "没用"}.get(r["feedback"] or "", "没反馈")
        why = f"（{REASON_TEXT.get(r['reason'], r['reason'])}）" if r["reason"] else ""
        out.append(f"- {r['item_id']} [{PLATFORM_TEXT.get(r['platform'], '')}] {r['title']} | 你的粗排分 "
                   f"{scores[0] if scores and scores[0] is not None else '-'} | 他：{fb}{why}")
    return "\n".join(out) or "（无）"


def run_review(llm, ledger: Ledger, profile_dir: Path, week_start: date) -> tuple[str, ReviewResult]:
    rows = _week_rows(ledger, week_start)
    stats = weekly_stats(ledger, week_start)
    understanding = Path(profile_dir) / "understanding.md"
    text = understanding.read_text(encoding="utf-8") if understanding.exists() else "（还没有）"
    stats_text = "\n".join(f"- {k}：{v}" for k, v in stats.items())
    msg = _prompt("review", stats=stats_text, understanding=text, main=_fmt_rows(ledger, rows["main"]),
                  missed=_fmt_rows(ledger, rows["missed"]), serendipity=_fmt_rows(ledger, rows["serendipity"]))
    res, _ = llm.complete_json([_system("（见下方复盘材料）"), {"role": "user", "content": msg}], ReviewResult)
    week = week_id(week_start)
    ledger.save_review(week, [p.model_dump() for p in res.proposals])
    return week, res


def apply_accepted(ledger: Ledger, profile_dir: Path, week: str) -> Path | None:
    rv = ledger.get_review(week)
    if not rv:
        return None
    accepted = [p for i, p in enumerate(rv["proposals"]) if rv["decisions"].get(str(i))]
    if not accepted:
        return None
    profile_dir = Path(profile_dir)
    target = profile_dir / "understanding.md"
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    new_lines = [f"- {p['change']}（{week} 采纳）" for p in accepted if f"- {p['change']}（{week} 采纳）" not in old]
    if not new_lines:
        return target
    if target.exists():
        hist = profile_dir / "history"
        hist.mkdir(exist_ok=True)
        shutil.copyfile(target, hist / f"understanding-{date.today().isoformat()}-{week}.md")
    target.write_text(old + ("" if not old or old.endswith("\n") else "\n") + "\n".join(new_lines) + "\n",
                      encoding="utf-8")
    return target

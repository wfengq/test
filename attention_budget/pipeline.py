"""One daily round: collect → search → coarse → deep → final, resumable at every step."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .agent.context import build_context
from .agent.stages import CHANNEL_TEXT, PLATFORM_TEXT, coarse_rank, deep_read, finalize, plan_searches
from .guard import CapReached, FinalPick, Guard
from .ledger import Ledger
from .models import Item, SourceFailure

STAGES = ["collect", "search", "coarse", "deep", "final", "done"]
SOURCE_NAME = {"bili": "B站", "xhs": "小红书"}


@dataclass
class RunResult:
    run_id: str
    picks: list[tuple[Item, FinalPick, dict]]
    missed: list[tuple[Item, dict]]
    stats: dict
    source_status: dict[str, str]
    capped: bool
    status: str
    day: str = ""
    extra: dict = field(default_factory=dict)


def _done(ledger: Ledger, run_id: str, stage: str) -> bool:
    cur = ledger.get_stage(run_id)
    return cur is not None and STAGES.index(cur) >= STAGES.index(stage)


def _bound_tools(item: Item, bili, xhs) -> dict:
    """Tools are bound to the item being read; the model only chooses which one to use."""
    if item.platform == "bili":
        t = bili.tools()
        bvid = item.id.split(":", 1)[1]
        return {"subtitle": lambda: t["subtitle"](bvid), "comments": lambda: t["comments"](bvid),
                "up_recent": lambda: t["up_recent"](item.author_id)}
    t = xhs.tools()
    return {"note_detail": lambda: t["note_detail"](item.id),
            "author_recent": lambda: t["author_recent"](item.author_id, item.xsec_token or "")}


def _collect(cfg, ledger: Ledger, run_id: str, bili, xhs, now: datetime, status: dict) -> None:
    since = ledger.last_run_started_at() or (now - timedelta(hours=24))
    v = cfg.volumes
    jobs = [("bili", lambda: bili.follow_updates(since, v.bili_follow)),
            ("bili", lambda: bili.recommend(v.bili_recommend)),
            ("xhs", lambda: xhs.recommend(v.xhs_recommend))]
    if cfg.xhs.key_creators:
        jobs.append(("xhs", lambda: xhs.key_creators_updates(cfg.xhs.key_creators, v.xhs_creator_notes)))
    for src, job in jobs:
        if src in status:
            continue
        try:
            ledger.save_items(run_id, job())
        except SourceFailure as e:
            status[src] = e.reason


def _search(cfg, ledger: Ledger, llm, guard: Guard, run_id: str, bili, xhs, ctx: str, status: dict) -> None:
    if len(status) == 2:
        return
    for q in plan_searches(llm, guard, ctx, cfg):
        if q["platform"] in status:
            continue
        try:
            if q["platform"] == "bili":
                found = bili.search(q["query"], cfg.volumes.results_per_search)
            else:
                found = xhs.search(q["query"], cfg.volumes.results_per_search, scope=q["scope"])
            ledger.save_items(run_id, found)
            ledger.add_tool_call(run_id, None, "search", q, True)
        except SourceFailure as e:
            ledger.add_tool_call(run_id, None, "search", q, False, e.reason)



def run_daily(cfg, ledger: Ledger, llm, bili, xhs, now: datetime, force: bool = False,
              profile_dir: Path | None = None) -> RunResult:
    run_id, _ = ledger.start_run(now.date(), force=force)
    guard = Guard(cfg.caps, cfg.budget, ledger, run_id)
    t_in, t_out, calls = ledger.usage(run_id)  # resumed rounds keep counting from where they stopped
    guard.tokens, guard.tool_calls = t_in + t_out, calls
    ctx = build_context(profile_dir or (cfg.data_dir / "profile"), ledger, now, cfg)
    status: dict = ledger.get_state(run_id, "source_status", {})
    capped = bool(ledger.get_state(run_id, "capped", False))

    if not _done(ledger, run_id, "collect"):
        _collect(cfg, ledger, run_id, bili, xhs, now, status)
        ledger.set_state(run_id, "source_status", status)
        ledger.set_stage(run_id, "collect")
    if not _done(ledger, run_id, "search"):
        _search(cfg, ledger, llm, guard, run_id, bili, xhs, ctx, status)
        ledger.set_stage(run_id, "search")
    items = {it.id: it for it in ledger.run_items(run_id)}
    xhs_items = [it for it in items.values() if it.platform == "xhs"]
    if xhs_items and "xhs" not in status:
        xhs.remember(xhs_items)

    if not items:
        status_word = "failed"
        result = RunResult(run_id, [], [], {"seen": 0, "deep_read": 0, "reject_summary": [], "tokens_in": t_in,
                                            "tokens_out": t_out, "cost": 0.0}, status, capped, status_word,
                           day=now.date().isoformat())
        ledger.finish_run(run_id, status_word, status, capped)
        ledger.set_stage(run_id, "done")
        return result

    if not _done(ledger, run_id, "coarse"):
        shortlist = coarse_rank(llm, guard, ledger, run_id, list(items.values()), ctx, cfg)
        ledger.set_state(run_id, "shortlist", shortlist)
        ledger.set_stage(run_id, "coarse")
    if not _done(ledger, run_id, "deep"):
        read = {v["item_id"] for v in ledger.verdicts(run_id, "deep")}
        for item_id in ledger.get_state(run_id, "shortlist", []):
            if item_id in read or item_id not in items:
                continue
            if guard.exhausted:
                capped = True
                break
            try:
                deep_read(llm, guard, ledger, run_id, items[item_id], _bound_tools(items[item_id], bili, xhs), ctx)
            except CapReached:
                capped = True
                break
        ledger.set_state(run_id, "capped", capped)
        ledger.set_stage(run_id, "deep")
    if not _done(ledger, run_id, "final"):
        picks, summary = finalize(llm, guard, ledger, run_id, ctx, cfg.budget)
        ledger.set_state(run_id, "final", [p.__dict__ for p in picks])
        ledger.set_state(run_id, "reject_summary", summary)
        ledger.set_stage(run_id, "final")

    picks = [FinalPick(**d) for d in ledger.get_state(run_id, "final", [])]
    deep = {v["item_id"]: v for v in ledger.verdicts(run_id, "deep")}
    picked = {p.item_id for p in picks}
    result_picks = [(items[p.item_id], p, deep[p.item_id]) for p in picks if p.item_id in items]
    missed = [(items[k], v) for k, v in deep.items() if v["decision"] in ("pick", "maybe") and k not in picked]
    t_in, t_out, _ = ledger.usage(run_id)
    cost = t_in / 1e6 * cfg.deepseek.price_in_per_m + t_out / 1e6 * cfg.deepseek.price_out_per_m
    stats = {"seen": len(items), "deep_read": len(deep), "reject_summary": ledger.get_state(run_id, "reject_summary", []),
             "tokens_in": t_in, "tokens_out": t_out, "cost": round(cost, 4)}
    status_word = "failed" if len(status) == 2 else ("partial" if status else "ok")
    ledger.finish_run(run_id, status_word, status, capped)
    ledger.set_stage(run_id, "done")
    return RunResult(run_id, result_picks, missed, stats, status, capped, status_word, day=now.date().isoformat())


def write_report(result: RunResult, reports_dir: Path) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    f = reports_dir / f"{result.day}.md"

    def line(it: Item, note: str, extra: str = "") -> str:
        return f"- [{PLATFORM_TEXT[it.platform]}·{CHANNEL_TEXT[it.channel]}] [{it.title}]({it.url}){extra}\n  {note}"

    out = [f"# 注意力预算 · {result.day}", ""]
    if result.source_status:
        out += ["## 来源状态", ""] + [f"- {SOURCE_NAME.get(k, k)}：{v}" for k, v in result.source_status.items()] + [""]
    out += [f"## 今天推给你的（{len(result.picks)} 条）", ""]
    for it, p, v in result.picks:
        tag = "（🎲 你没关注的：" + p.reason + "）" if p.serendipity else ""
        out.append(line(it, v.get("note_to_user", ""), tag + ("（没读到原文）" if v.get("no_source") else "")))
    out += ["", f"## 值得看但没名额（{len(result.missed)} 条）", ""]
    out += [line(it, v.get("note_to_user", "")) for it, v in result.missed] or ["（无）"]
    s = result.stats
    out += ["", "## 账单", "", f"- 看了 {s['seen']} 条，深读 {s['deep_read']} 条" + ("（今天触到上限，深读提前结束）" if result.capped else "")]
    for r in s.get("reject_summary", []):
        out.append(f"- 筛掉：{r['category']} {r['count']} 条")
    out.append(f"- token：输入 {s['tokens_in']}，输出 {s['tokens_out']}；花费约 ¥{s['cost']}")
    f.write_text("\n".join(out) + "\n", encoding="utf-8")
    return f

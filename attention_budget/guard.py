"""The five boundaries the AI cannot cross: quota, bookkeeping, spend, pace, read-only."""
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable

from .config import Budget, Caps
from .ledger import Ledger

ITEM_CAP_MSG = "已达本条工具调用上限，请直接给出结论"


class CapReached(Exception):
    pass


@dataclass
class FinalPick:
    item_id: str
    rank: int
    serendipity: bool
    reason: str


class Guard:
    def __init__(self, caps: Caps, budget: Budget, ledger: Ledger, run_id: str,
                 clock: Callable[[], float] = time.monotonic):
        self.caps, self.budget, self.ledger, self.run_id = caps, budget, ledger, run_id
        self.clock = clock
        self.started = clock()
        self.tokens = 0
        self.tool_calls = 0
        self.per_item: Counter[str] = Counter()

    def charge(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens += tokens_in + tokens_out
        self.ledger.add_usage(self.run_id, tokens_in, tokens_out)

    @property
    def exhausted(self) -> bool:
        return (self.tokens >= self.caps.tokens
                or self.tool_calls >= self.caps.tool_calls
                or self.clock() - self.started >= self.caps.minutes * 60)

    def call_tool(self, item_id: str | None, name: str, fn: Callable[..., str], **args) -> str:
        if self.exhausted:
            raise CapReached(f"tokens={self.tokens} tool_calls={self.tool_calls}")
        if item_id is not None and self.per_item[item_id] >= self.caps.per_item_tools:
            return ITEM_CAP_MSG
        self.tool_calls += 1
        if item_id is not None:
            self.per_item[item_id] += 1
        try:
            out = fn(**args)
        except Exception as e:  # tools are untrusted I/O; the round must go on
            self.ledger.add_tool_call(self.run_id, item_id, name, args, False, str(e))
            return f"工具失败：{e}"
        self.ledger.add_tool_call(self.run_id, item_id, name, args, True)
        return out


def _problems(p: FinalPick, deep_read_ids: set[str], pushed_ids: set[str]) -> str | None:
    if p.item_id not in deep_read_ids:
        return f"{p.item_id} 不在深读过的条目里"
    if p.item_id in pushed_ids:
        return f"{p.item_id} 以前推送过"
    return None


def validate_final(picks: list[FinalPick], deep_read_ids: set[str], pushed_ids: set[str],
                   channel_of: dict[str, str], budget: Budget) -> list[str]:
    issues = [msg for p in picks if (msg := _problems(p, deep_read_ids, pushed_ids))]
    if len(picks) > budget.daily_picks:
        issues.append(f"选了 {len(picks)} 条，最多 {budget.daily_picks} 条")
    seren = [p for p in picks if p.serendipity]
    if len(seren) > budget.serendipity:
        issues.append(f"意外发现选了 {len(seren)} 条，最多 {budget.serendipity} 条")
    for p in seren:
        if channel_of.get(p.item_id) == "follow":
            issues.append(f"{p.item_id} 来自已关注的人，不能当意外发现")
    if len({p.item_id for p in picks}) != len(picks):
        issues.append("有重复条目")
    return issues


def enforce_final(picks: list[FinalPick], deep_read_ids: set[str], pushed_ids: set[str],
                  channel_of: dict[str, str], budget: Budget) -> list[FinalPick]:
    out: list[FinalPick] = []
    seen: set[str] = set()
    seren_left = budget.serendipity
    for p in sorted(picks, key=lambda p: p.rank):
        if p.item_id in seen or _problems(p, deep_read_ids, pushed_ids):
            continue
        if p.serendipity:
            if seren_left > 0 and channel_of.get(p.item_id) != "follow":
                seren_left -= 1
            else:
                p = replace(p, serendipity=False)
        seen.add(p.item_id)
        out.append(p)
        if len(out) == budget.daily_picks:
            break
    return out

"""Test doubles shared across test modules."""
from collections import defaultdict

from attention_budget.llm import LLMError, Usage


class FakeLLM:
    """Replays scripted answers keyed by schema class name."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list[dict]]] = []
        self._fail: dict[str, int] = {}
        self._seen = defaultdict(int)

    def complete_json(self, messages, schema):
        name = schema.__name__
        self.calls.append((name, list(messages)))
        self._seen[name] += 1
        if name in self._fail and self._seen[name] > self._fail[name]:
            raise LLMError(f"scripted failure in {name}")
        queue = self.script.get(name)
        if not queue:
            raise AssertionError(f"FakeLLM has no scripted answer for {name}")
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        return schema.model_validate(nxt), Usage(100, 10)

    def fail_on(self, name: str, after: int) -> None:
        self._fail[name] = after

    def healed(self) -> "FakeLLM":
        self._fail.clear()
        return self

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

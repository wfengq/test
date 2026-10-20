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


class FakeOcr:
    """Same call shape as rapidocr_onnxruntime.RapidOCR: returns ([[box, text, score], ...], elapse)."""

    def __init__(self, mapping: dict[bytes, list[str]]):
        self.mapping = mapping
        self.calls = 0

    def __call__(self, img: bytes):
        self.calls += 1
        lines = self.mapping.get(img)
        if lines is None:
            return None, 0.0
        return [[[[0, i], [1, i], [1, i + 1], [0, i + 1]], t, 0.9] for i, t in enumerate(lines)], 0.01


class FakeCollector:
    """Returns scripted Items per method name, or raises SourceFailure after fail_all()."""

    def __init__(self, platform: str, script: dict[str, list], tool_text: str = "原文内容"):
        self.platform = platform
        self.script = script
        self.tool_text = tool_text
        self.failure: str | None = None
        self.calls: list[str] = []
        self.remembered: list[str] = []

    def fail_all(self, reason: str) -> None:
        self.failure = reason

    def _get(self, name):
        from attention_budget.models import SourceFailure
        self.calls.append(name)
        if self.failure:
            raise SourceFailure(self.platform, self.failure)
        return list(self.script.get(name, []))

    def follow_updates(self, since, limit):
        return self._get("follow_updates")

    def recommend(self, limit):
        return self._get("recommend")

    def search(self, query, limit, scope="all"):
        return self._get("search")

    def key_creators_updates(self, links, per_creator):
        return self._get("key_creators_updates")

    def remember(self, items):
        self.remembered += [i.id for i in items]

    def tools(self):
        names = ["subtitle", "comments", "up_recent"] if self.platform == "bili" else ["note_detail", "author_recent"]
        return {n: (lambda *a, **k: self.tool_text) for n in names}

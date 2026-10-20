from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from attention_budget.agent.schemas import SearchPlan
from attention_budget.config import DeepSeekCfg
from attention_budget.llm import DeepSeekLLM, LLMError
from tests.fakes import FakeLLM


def cfg():
    return DeepSeekCfg(api_key="k")


class StubClient:
    """Mimics openai.OpenAI().chat.completions.create."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        msg = SimpleNamespace(content=r)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3))


def conn_err():
    return APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com"))


def test_reasks_once_on_invalid_json_then_succeeds():
    client = StubClient(["not json", '{"queries": []}'])
    llm = DeepSeekLLM(cfg(), client=client, sleep=lambda s: None)
    out, usage = llm.complete_json([{"role": "user", "content": "x"}], SearchPlan)
    assert out.queries == [] and len(client.requests) == 2
    assert "不符合格式" in client.requests[1]["messages"][-1]["content"]
    assert (usage.tokens_in, usage.tokens_out) == (20, 6)


def test_second_invalid_answer_raises():
    client = StubClient(["no", "still no"])
    with pytest.raises(LLMError):
        DeepSeekLLM(cfg(), client=client, sleep=lambda s: None).complete_json([], SearchPlan)


def test_retries_network_errors_three_times_then_raises():
    client = StubClient([conn_err()] * 4)
    waits = []
    with pytest.raises(LLMError):
        DeepSeekLLM(cfg(), client=client, sleep=waits.append).complete_json([], SearchPlan)
    assert len(client.requests) == 4 and waits == [1, 2, 4]


def test_requests_json_mode():
    client = StubClient(['{"queries": []}'])
    DeepSeekLLM(cfg(), client=client, sleep=lambda s: None).complete_json([], SearchPlan)
    assert client.requests[0]["response_format"] == {"type": "json_object"}
    assert client.requests[0]["model"] == "deepseek-chat"


def test_fake_llm_replays_by_schema_name():
    llm = FakeLLM({"SearchPlan": [{"queries": []}]})
    out, _ = llm.complete_json([{"role": "user", "content": "hi"}], SearchPlan)
    assert out.queries == [] and llm.calls[0][0] == "SearchPlan"

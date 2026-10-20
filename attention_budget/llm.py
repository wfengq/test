"""DeepSeek client that always returns a validated pydantic object."""
import json
import time
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

import openai
from pydantic import BaseModel, ValidationError

from .config import DeepSeekCfg

T = TypeVar("T", bound=BaseModel)
BACKOFF = [1, 2, 4]


@dataclass
class Usage:
    tokens_in: int
    tokens_out: int


class LLMError(Exception):
    pass


class LLMFormatError(LLMError):
    """The model answered, but twice not in the required shape."""


class LLM(Protocol):
    def complete_json(self, messages: list[dict], schema: type[T]) -> tuple[T, Usage]: ...


class DeepSeekLLM:
    def __init__(self, cfg: DeepSeekCfg, sleep: Callable[[float], None] = time.sleep, client=None):
        self.cfg = cfg
        self.sleep = sleep
        self.client = client or openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, max_retries=0)

    def _call(self, messages: list[dict]):
        for attempt in range(len(BACKOFF) + 1):
            try:
                return self.client.chat.completions.create(
                    model=self.cfg.model, messages=messages, response_format={"type": "json_object"})
            except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError,
                    openai.InternalServerError) as e:
                if attempt == len(BACKOFF):
                    raise LLMError(f"DeepSeek 连不上：{e}") from e
                self.sleep(BACKOFF[attempt])
            except openai.APIError as e:
                raise LLMError(f"DeepSeek 报错：{e}") from e

    def complete_json(self, messages: list[dict], schema: type[T]) -> tuple[T, Usage]:
        messages = list(messages)
        usage = Usage(0, 0)
        for attempt in range(2):
            resp = self._call(messages)
            if resp.usage:
                usage.tokens_in += resp.usage.prompt_tokens
                usage.tokens_out += resp.usage.completion_tokens
            text = resp.choices[0].message.content or ""
            try:
                return schema.model_validate(json.loads(text)), usage
            except (json.JSONDecodeError, ValidationError) as e:
                if attempt == 1:
                    raise LLMFormatError(f"DeepSeek 两次输出都不符合格式：{e}") from e
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"上次输出不符合格式：{e}，请只输出符合要求的 JSON"}]
        raise AssertionError("unreachable")

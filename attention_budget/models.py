"""Shared data shapes for every source."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

SNIPPET_MAX = 300


class Item(BaseModel):
    id: str
    platform: Literal["bili", "xhs"]
    channel: Literal["follow", "recommend", "search"]
    author: str
    author_id: str = ""
    title: str
    snippet: str
    url: str
    published_at: datetime | None
    metrics: dict[str, int] = {}
    query: str | None = None
    xsec_token: str | None = None

    @field_validator("snippet")
    @classmethod
    def _truncate(cls, v: str) -> str:
        return v[:SNIPPET_MAX]


class SourceFailure(Exception):
    """A whole source is unusable this round; other sources keep running."""

    def __init__(self, source: str, reason: str):
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason

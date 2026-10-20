"""Fixed output shapes for every AI step."""
from typing import Literal

from pydantic import BaseModel


class SearchQuery(BaseModel):
    platform: Literal["bili", "xhs"]
    scope: Literal["all", "followed"] = "all"
    query: str
    reason: str = ""


class SearchPlan(BaseModel):
    queries: list[SearchQuery]

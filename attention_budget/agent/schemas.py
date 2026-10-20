"""Fixed output shapes for every AI step."""
from typing import Literal

from pydantic import BaseModel, Field


class SearchQuery(BaseModel):
    platform: Literal["bili", "xhs"]
    scope: Literal["all", "followed"] = "all"
    query: str
    reason: str = ""


class SearchPlan(BaseModel):
    queries: list[SearchQuery]


class Score(BaseModel):
    id: str
    score: int = Field(ge=0, le=100)
    reason: str = ""


class CoarseResult(BaseModel):
    scores: list[Score]
    rescue: list[str] = []


class DeepStep(BaseModel):
    action: Literal["tool", "verdict"]
    tool: str | None = None
    args: dict = {}
    decision: Literal["pick", "maybe", "reject"] | None = None
    reason: str = ""
    note_to_user: str = ""
    no_source: bool = False


class RankedId(BaseModel):
    id: str
    rank: int


class Serendipity(BaseModel):
    id: str
    reason: str = ""


class RejectCount(BaseModel):
    category: str
    count: int


class FinalResult(BaseModel):
    picks: list[RankedId]
    serendipity: Serendipity | None = None
    reject_summary: list[RejectCount] = []


class Proposal(BaseModel):
    change: str
    reason: str = ""
    evidence_ids: list[str] = []


class ReviewResult(BaseModel):
    proposals: list[Proposal]
    new_interest_suggestions: list[str] = []

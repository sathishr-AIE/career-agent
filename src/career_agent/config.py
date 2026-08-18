import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class CareerBrief(BaseModel):
    target_titles: list[str] = Field(min_length=1)
    title_families: list[str] = Field(default_factory=list)

    # Drives what we ASK each source for.
    search_locations: list[str] = Field(min_length=1)
    # Accepted on the way back by the hard filter. Usually a superset.
    locations: list[str] = Field(default_factory=list)

    remote_ok: bool = True
    salary_floor_inr: int | None = None
    work_authorization: list[str] = Field(default_factory=list)
    daily_cap: int = Field(default=5, ge=1)
    gate_threshold: int = Field(default=72, ge=0, le=100)
    staleness_days: int = Field(default=30, ge=1)
    excluded_companies: list[str] = Field(default_factory=list)
    non_negotiables: list[str] = Field(default_factory=list)


class Board(BaseModel):
    provider: str
    token: str
    company: str
    tier: int = 2


def load_brief(path: Path) -> CareerBrief:
    with open(path, "rb") as f:
        return CareerBrief(**tomllib.load(f))


def load_boards(path: Path) -> list[Board]:
    with open(path, "rb") as f:
        return [Board(**b) for b in tomllib.load(f).get("board", [])]

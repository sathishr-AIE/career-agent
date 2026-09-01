import os
import tomllib
from pathlib import Path

import tomlkit
from pydantic import BaseModel, Field

# Operational, not part of the career brief. Validated in Python rather than
# by a CHECK constraint so retiring or adding a model is a constant edit, not
# a schema migration. Undated aliases only: dated snapshots get retired out
# from under stored rows, aliases don't.
SCORING_MODELS = ("claude-sonnet-5", "claude-haiku-4-5")
DEFAULT_SCORING_MODEL = SCORING_MODELS[0]

MODEL_LABELS = {
    "claude-sonnet-5": "Sonnet — better judgement (default)",
    "claude-haiku-4-5": "Haiku — faster, lighter on rate limits",
}


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


class CandidateProfile(BaseModel):
    """PII, deliberately kept out of CareerBrief/career_brief.toml, which is
    version-controlled. See candidate_profile.toml.example."""
    candidate_name: str = Field(min_length=1)
    candidate_email: str = Field(min_length=1)
    candidate_phone: str = Field(min_length=1)
    linkedin_url: str | None = None
    portfolio_url: str | None = None


def load_brief(path: Path) -> CareerBrief:
    with open(path, "rb") as f:
        return CareerBrief(**tomllib.load(f))


def load_boards(path: Path) -> list[Board]:
    with open(path, "rb") as f:
        return [Board(**b) for b in tomllib.load(f).get("board", [])]


def _save_toml(path: Path, values: dict) -> None:
    """Write-then-rename TOML save, shared by save_brief and
    save_candidate_profile. Preserves comments, key order, and formatting on
    an existing file. Write-then-rename, not write_text: write_text
    truncates in place, and readers (the worker's guard(), every dashboard
    route) call the matching load_* function on every tick and every
    request. A reader landing in that truncate window gets half a TOML and
    raises. os.replace is atomic on Windows and POSIX; same directory keeps
    it a rename."""
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    for field, value in values.items():
        if value is None:
            # TOML has no null; absent is how "unset" is spelled, and the
            # matching load_* will fall back to the pydantic default.
            if field in doc:
                doc.pop(field)
        elif field not in doc or doc[field] != value:
            # Assigning unconditionally would replace the item wholesale,
            # discarding its original formatting (e.g. a manually wrapped
            # multi-line array) even when the value didn't change.
            doc[field] = value

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_brief(path: Path, brief: CareerBrief) -> None:
    """The file is version controlled and its comments explain non-obvious
    consequences ("adding a city multiplies daily Actor runs"), so a
    round-trip write is the only acceptable kind."""
    _save_toml(path, brief.model_dump())


def load_candidate_profile(path: Path) -> CandidateProfile:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy candidate_profile.toml.example to "
            f"{path.name} and fill in your details -- required before any "
            "real Greenhouse submission can run.")
    with open(path, "rb") as f:
        return CandidateProfile(**tomllib.load(f))


def save_candidate_profile(path: Path, profile: CandidateProfile) -> None:
    _save_toml(path, profile.model_dump())

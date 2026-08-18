from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class Job(BaseModel):
    source: Literal["ats", "linkedin", "naukri"]
    external_id: str
    company: str
    title: str
    location: str | None = None
    is_remote: bool = False
    comp_min: int | None = None
    comp_max: int | None = None
    posted_at: str | None = None
    url: str | None = None
    description: str | None = None


class Verdict(BaseModel):
    role_fit: int = Field(ge=0, le=100)
    credibility: int = Field(ge=0, le=100)
    opportunity: int = Field(ge=0, le=100)
    application_quality: int = Field(ge=0, le=100)
    eligibility_soft: int = Field(ge=0, le=100)
    verdict: Literal["submit", "hold", "skip"]
    rationale: str

    WEIGHTS: ClassVar[dict[str, float]] = {
        "role_fit": 0.30, "credibility": 0.30, "opportunity": 0.20,
        "application_quality": 0.15, "eligibility_soft": 0.05,
    }

    @property
    def weighted(self) -> float:
        return round(sum(getattr(self, k) * w for k, w in self.WEIGHTS.items()), 2)

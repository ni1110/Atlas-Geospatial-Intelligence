"""Candidate-site domain models."""

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class CandidateStage(StrEnum):
    """The screening stage that produced a candidate site."""

    PRELIMINARY_NON_WATER = "preliminary_non_water"


class CandidateScreeningStatus(StrEnum):
    """Current business screening state of a candidate site."""

    NOT_EVALUATED = "not_evaluated"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class CandidateSite(BaseModel):
    """A traceable polygon component entering the screening pipeline."""

    candidate_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)

    rank: int = Field(ge=1)

    stage: CandidateStage = (
        CandidateStage.PRELIMINARY_NON_WATER
    )
    screening_status: CandidateScreeningStatus = (
        CandidateScreeningStatus.NOT_EVALUATED
    )

    area_km2: float = Field(gt=0)

    centroid_lon: float = Field(ge=-180, le=180)
    centroid_lat: float = Field(ge=-90, le=90)

    geometry_sha256: str = Field(
        min_length=64,
        max_length=64,
    )

    artifact_path: str = Field(min_length=1)
    source_evidence_id: str = Field(min_length=1)

    created_at: datetime = Field(default_factory=utc_now)

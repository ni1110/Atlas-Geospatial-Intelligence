"""Evidence and provenance models."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceType(StrEnum):
    ADMINISTRATIVE_BOUNDARY = "administrative_boundary"
    WATER_EXCLUSION = "water_exclusion"
    NON_WATER_AREA = "non_water_area"
    CANDIDATE_SET = "candidate_set"


class Evidence(BaseModel):
    """A traceable artifact used to support a site-screening decision."""

    evidence_id: str = Field(
        default_factory=lambda: str(uuid4())
    )
    evidence_type: EvidenceType
    provider: str
    source_identifier: str
    artifact_path: str
    checksum_sha256: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    source_license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

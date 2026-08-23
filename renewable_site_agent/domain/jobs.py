"""Site-selection job domain model."""

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from renewable_site_agent.models import SiteSelectionRequest
from .evidence import Evidence


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    RESOLVING_BOUNDARY = "resolving_boundary"
    BOUNDARY_READY = "boundary_ready"
    PREPARING_WATER = "preparing_water"
    WATER_READY = "water_ready"
    PREPARING_NON_WATER = "preparing_non_water"
    NON_WATER_READY = "non_water_ready"
    BUILDING_CANDIDATES = "building_candidates"
    CANDIDATES_READY = "candidates_ready"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.RECEIVED: {
        JobStatus.PARSING,
        JobStatus.FAILED,
    },
    JobStatus.PARSING: {
        JobStatus.RESOLVING_BOUNDARY,
        JobStatus.FAILED,
    },
    JobStatus.RESOLVING_BOUNDARY: {
        JobStatus.BOUNDARY_READY,
        JobStatus.FAILED,
    },
    JobStatus.BOUNDARY_READY: {
        JobStatus.PREPARING_WATER,
        JobStatus.FAILED,
    },
    JobStatus.PREPARING_WATER: {
        JobStatus.WATER_READY,
        JobStatus.FAILED,
    },
    JobStatus.WATER_READY: {
        JobStatus.PREPARING_NON_WATER,
        JobStatus.FAILED,
    },
    JobStatus.PREPARING_NON_WATER: {
        JobStatus.NON_WATER_READY,
        JobStatus.FAILED,
    },
    JobStatus.NON_WATER_READY: {
        JobStatus.BUILDING_CANDIDATES,
        JobStatus.FAILED,
    },
    JobStatus.BUILDING_CANDIDATES: {
        JobStatus.CANDIDATES_READY,
        JobStatus.FAILED,
    },
    JobStatus.CANDIDATES_READY: set(),
    JobStatus.FAILED: set(),
}


class JobError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class SiteSelectionJob(BaseModel):
    """Persisted unit of work for one site-screening request."""

    model_config = ConfigDict(validate_assignment=True)

    job_id: str = Field(default_factory=lambda: str(uuid4()))
    version: int = 1
    status: JobStatus = JobStatus.RECEIVED

    request_text: str = Field(min_length=1)
    request: SiteSelectionRequest | None = None

    evidence: list[Evidence] = Field(default_factory=list)
    error: JobError | None = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def transition(self, next_status: JobStatus) -> None:
        allowed = ALLOWED_TRANSITIONS[self.status]

        if next_status not in allowed:
            raise ValueError(
                f"Invalid job transition: "
                f"{self.status.value} -> {next_status.value}"
            )

        self.status = next_status
        self.updated_at = utc_now()

    def fail(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        self.status = JobStatus.FAILED
        self.error = JobError(
            code=code,
            message=message,
            retryable=retryable,
        )
        self.updated_at = utc_now()

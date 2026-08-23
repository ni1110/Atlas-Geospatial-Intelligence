"""Create the initial non-water area for one site-selection job."""

import hashlib
from pathlib import Path

from renewable_site_agent.adapters.spatial_difference import (
    SpatialDifferenceEngine,
)
from renewable_site_agent.application.create_job import (
    PROJECT_ROOT,
    RUNTIME_ROOT,
)
from renewable_site_agent.domain.evidence import (
    Evidence,
    EvidenceType,
)
from renewable_site_agent.domain.jobs import (
    JobStatus,
    SiteSelectionJob,
)
from renewable_site_agent.infrastructure.job_store import (
    SQLiteJobStore,
)


def _artifact_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def prepare_non_water_area(
    job_id: str,
    *,
    store: SQLiteJobStore | None = None,
) -> SiteSelectionJob:
    """Subtract verified water polygons from the job boundary."""

    store = store or SQLiteJobStore(
        RUNTIME_ROOT / "site_jobs.db"
    )

    job = store.get(job_id)

    existing = next(
        (
            item
            for item in job.evidence
            if item.evidence_type
            == EvidenceType.NON_WATER_AREA
        ),
        None,
    )

    if (
        job.status == JobStatus.NON_WATER_READY
        and existing is not None
        and _artifact_path(existing.artifact_path).exists()
    ):
        return job

    if job.status != JobStatus.WATER_READY:
        raise ValueError(
            "Non-water preparation requires status water_ready; "
            f"received {job.status.value}."
        )

    boundary_evidence = next(
        item
        for item in job.evidence
        if item.evidence_type
        == EvidenceType.ADMINISTRATIVE_BOUNDARY
    )

    water_evidence = next(
        item
        for item in job.evidence
        if item.evidence_type
        == EvidenceType.WATER_EXCLUSION
    )

    output_path = (
        RUNTIME_ROOT
        / "jobs"
        / job.job_id
        / "non_water_area.geojson"
    )

    try:
        job.transition(JobStatus.PREPARING_NON_WATER)

        store.save(
            job,
            event_type="non_water_difference_started",
            event_payload={
                "boundary_evidence_id": (
                    boundary_evidence.evidence_id
                ),
                "water_evidence_id": (
                    water_evidence.evidence_id
                ),
                "operation": "difference",
            },
        )

        result = SpatialDifferenceEngine().subtract(
            boundary_path=_artifact_path(
                boundary_evidence.artifact_path
            ),
            exclusion_path=_artifact_path(
                water_evidence.artifact_path
            ),
            output_path=output_path,
        )

        job.evidence.append(
            Evidence(
                evidence_type=EvidenceType.NON_WATER_AREA,
                provider="Derived GIS analysis",
                source_identifier=(
                    "difference:"
                    f"{boundary_evidence.evidence_id}:"
                    f"{water_evidence.evidence_id}"
                ),
                artifact_path=_relative_path(
                    result.artifact_path
                ),
                checksum_sha256=_sha256_file(
                    result.artifact_path
                ),
                source_license=(
                    "Derived from geoBoundaries CC BY 4.0 "
                    "and OpenStreetMap ODbL 1.0"
                ),
                metadata={
                    "operation": "difference",
                    "boundary_evidence_id": (
                        boundary_evidence.evidence_id
                    ),
                    "water_evidence_id": (
                        water_evidence.evidence_id
                    ),
                    "part_count": result.part_count,
                    "boundary_area_km2": round(
                        result.boundary_area_km2,
                        6,
                    ),
                    "excluded_water_area_km2": round(
                        result.excluded_area_km2,
                        6,
                    ),
                    "result_area_km2": round(
                        result.result_area_km2,
                        6,
                    ),
                    "overlap_area_km2": round(
                        result.overlap_area_km2,
                        9,
                    ),
                    "conservation_error_km2": round(
                        result.conservation_error_km2,
                        9,
                    ),
                    "metric_crs": result.metric_crs,
                },
            )
        )

        job.transition(JobStatus.NON_WATER_READY)

        store.save(
            job,
            event_type="non_water_difference_completed",
            event_payload={
                "artifact_path": _relative_path(
                    result.artifact_path
                ),
                "part_count": result.part_count,
                "result_area_km2": round(
                    result.result_area_km2,
                    6,
                ),
                "overlap_area_km2": round(
                    result.overlap_area_km2,
                    9,
                ),
            },
        )

        return job

    except Exception as exc:
        job.fail(
            code=type(exc).__name__,
            message=str(exc),
            retryable=False,
        )

        store.save(
            job,
            event_type="job_failed",
            event_payload={
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "retryable": False,
            },
        )

        raise

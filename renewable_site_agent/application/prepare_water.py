"""Prepare the real water exclusion evidence for one job."""

import hashlib
from pathlib import Path

from renewable_site_agent.adapters.osm_water import (
    GeofabrikWaterProvider,
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _find_default_gpkg() -> Path:
    files = sorted(
        (RUNTIME_ROOT / "reference" / "osm").glob("*.gpkg")
    )

    if not files:
        raise FileNotFoundError(
            "No GeoPackage found under runtime/reference/osm."
        )

    if len(files) > 1:
        raise RuntimeError(
            "Multiple GeoPackages found; specify the input explicitly."
        )

    return files[0]


def prepare_water_exclusion(
    job_id: str,
    *,
    store: SQLiteJobStore | None = None,
    gpkg_path: Path | None = None,
) -> SiteSelectionJob:
    """Extract and persist the water exclusion layer for one job."""

    store = store or SQLiteJobStore(
        RUNTIME_ROOT / "site_jobs.db"
    )

    job = store.get(job_id)

    if job.status != JobStatus.BOUNDARY_READY:
        raise ValueError(
            "Water preparation requires job status "
            f"boundary_ready, received {job.status.value}."
        )

    boundary_evidence = next(
        (
            item
            for item in job.evidence
            if item.evidence_type
            == EvidenceType.ADMINISTRATIVE_BOUNDARY
        ),
        None,
    )

    if boundary_evidence is None:
        raise ValueError(
            "The job has no administrative boundary evidence."
        )

    boundary_path = (
        PROJECT_ROOT / boundary_evidence.artifact_path
    )

    gpkg_path = gpkg_path or _find_default_gpkg()

    output_path = (
        RUNTIME_ROOT
        / "jobs"
        / job.job_id
        / "water_exclusion.geojson"
    )

    try:
        job.transition(JobStatus.PREPARING_WATER)

        store.save(
            job,
            event_type="water_extraction_started",
            event_payload={
                "dataset_path": _relative_path(gpkg_path),
                "source_layer": "gis_osm_water_a_free",
            },
        )

        result = GeofabrikWaterProvider(
            gpkg_path
        ).extract(
            boundary_path=boundary_path,
            output_path=output_path,
        )

        job.evidence.append(
            Evidence(
                evidence_type=EvidenceType.WATER_EXCLUSION,
                provider="Geofabrik / OpenStreetMap",
                source_identifier=(
                    f"{gpkg_path.name}:"
                    f"{result.source_layer}"
                ),
                artifact_path=_relative_path(
                    result.artifact_path
                ),
                checksum_sha256=_sha256_file(
                    result.artifact_path
                ),
                source_license="ODbL 1.0",
                metadata={
                    "dataset_path": _relative_path(gpkg_path),
                    "source_layer": result.source_layer,
                    "source_attribution": (
                        "© OpenStreetMap contributors"
                    ),
                    "input_feature_count": (
                        result.input_feature_count
                    ),
                    "output_feature_count": (
                        result.feature_count
                    ),
                    "total_area_km2": round(
                        result.total_area_km2,
                        6,
                    ),
                    "metric_crs": result.metric_crs,
                },
            )
        )

        job.transition(JobStatus.WATER_READY)

        store.save(
            job,
            event_type="water_extraction_completed",
            event_payload={
                "artifact_path": _relative_path(
                    result.artifact_path
                ),
                "feature_count": result.feature_count,
                "total_area_km2": round(
                    result.total_area_km2,
                    6,
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

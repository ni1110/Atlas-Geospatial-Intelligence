"""Create a persisted site-selection job and resolve its real boundary."""

import hashlib
import json
import os
from pathlib import Path
from urllib.error import URLError

from renewable_site_agent.adapters.admin_boundary import (
    BoundaryResolutionError,
    NominatimBoundaryProvider,
)
from renewable_site_agent.adapters.local_admin_boundary import (
    LocalGeoBoundariesProvider,
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
from renewable_site_agent.parser import (
    parse_site_selection_request,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = Path(
    os.getenv(
        "RENEWABLE_SITE_RUNTIME_DIR",
        str(PROJECT_ROOT / "runtime"),
    )
)


def _write_json_atomic(
    destination: Path,
    payload: dict,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(destination)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def _relative_artifact_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def create_site_selection_job(
    request_text: str,
    *,
    store: SQLiteJobStore | None = None,
    boundary_provider: (
        LocalGeoBoundariesProvider
        | NominatimBoundaryProvider
        | None
    ) = None,
) -> SiteSelectionJob:
    """Create a job, parse constraints, and resolve a real boundary."""

    store = store or SQLiteJobStore(
        RUNTIME_ROOT / "site_jobs.db"
    )

    boundary_provider = (
        boundary_provider
        or LocalGeoBoundariesProvider(
            dataset_path=(
                RUNTIME_ROOT
                / "reference"
                / "boundaries"
                / "geoBoundaries-CHN-ADM1.geojson"
            )
        )
    )

    job = SiteSelectionJob(request_text=request_text)

    store.save(
        job,
        event_type="job_received",
    )

    try:
        job.transition(JobStatus.PARSING)

        store.save(
            job,
            event_type="constraint_parsing_started",
        )

        job.request = parse_site_selection_request(
            request_text
        )

        store.save(
            job,
            event_type="constraint_parsing_completed",
            event_payload={
                "region": job.request.region,
                "project_type": job.request.project_type,
                "capacity_mw": job.request.capacity_mw,
            },
        )

        job.transition(JobStatus.RESOLVING_BOUNDARY)

        store.save(
            job,
            event_type="boundary_resolution_started",
            event_payload={
                "region": job.request.region,
            },
        )

        boundary = boundary_provider.resolve(
            job.request.region
        )

        feature_collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "job_id": job.job_id,
                        "region": job.request.region,
                        "display_name": boundary.display_name,
                        "source_provider": boundary.metadata.get("provider", "unknown"),
                        "source_identifier": (
                            boundary.source_identifier
                        ),
                        "source_license": (
                            boundary.source_license
                        ),
                    },
                    "geometry": boundary.geometry,
                }
            ],
        }

        artifact_path = (
            RUNTIME_ROOT
            / "jobs"
            / job.job_id
            / "administrative_boundary.geojson"
        )

        _write_json_atomic(
            artifact_path,
            feature_collection,
        )

        job.evidence.append(
            Evidence(
                evidence_type=(
                    EvidenceType.ADMINISTRATIVE_BOUNDARY
                ),
                provider=boundary.metadata.get("provider", "unknown"),
                source_identifier=(
                    boundary.source_identifier
                ),
                artifact_path=(
                    _relative_artifact_path(artifact_path)
                ),
                checksum_sha256=(
                    _sha256_file(artifact_path)
                ),
                source_license=boundary.source_license,
                metadata={
                    "display_name": boundary.display_name,
                    "bounding_box": boundary.bounding_box,
                    **boundary.metadata,
                },
            )
        )

        job.transition(JobStatus.BOUNDARY_READY)

        store.save(
            job,
            event_type="boundary_resolution_completed",
            event_payload={
                "artifact_path": (
                    _relative_artifact_path(artifact_path)
                ),
                "source_identifier": (
                    boundary.source_identifier
                ),
            },
        )

        return job

    except Exception as exc:
        retryable = isinstance(
            exc,
            (
                URLError,
                TimeoutError,
                BoundaryResolutionError,
            ),
        )

        job.fail(
            code=type(exc).__name__,
            message=str(exc),
            retryable=retryable,
        )

        store.save(
            job,
            event_type="job_failed",
            event_payload={
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "retryable": retryable,
            },
        )

        raise

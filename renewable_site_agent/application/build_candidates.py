"""Build and persist preliminary candidate sites from non-water polygons."""

import hashlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import geopandas as gpd
from shapely import normalize, to_wkb

from renewable_site_agent.application.create_job import (
    PROJECT_ROOT,
    RUNTIME_ROOT,
)
from renewable_site_agent.domain.candidates import CandidateSite
from renewable_site_agent.domain.evidence import (
    Evidence,
    EvidenceType,
)
from renewable_site_agent.domain.jobs import (
    JobStatus,
    SiteSelectionJob,
)
from renewable_site_agent.infrastructure.candidate_store import (
    SQLiteCandidateStore,
)
from renewable_site_agent.infrastructure.job_store import (
    SQLiteJobStore,
)


def _resolve_artifact_path(value: str) -> Path:
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
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _geometry_sha256(geometry: object) -> str:
    """Return a reproducible hash for a normalized two-dimensional geometry."""
    normalized_geometry = normalize(geometry)

    canonical_wkb = to_wkb(
        normalized_geometry,
        hex=False,
        output_dimension=2,
        byte_order=1,
        include_srid=False,
    )

    return hashlib.sha256(canonical_wkb).hexdigest()


def _candidate_id(
    *,
    job_id: str,
    geometry_sha256: str,
) -> str:
    """Generate a stable candidate identifier for one job and geometry."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"renewable-site-agent:{job_id}:{geometry_sha256}",
        )
    )


def build_candidate_sites(
    job_id: str,
    *,
    job_store: SQLiteJobStore | None = None,
    candidate_store: SQLiteCandidateStore | None = None,
) -> SiteSelectionJob:
    """
    Convert non-water polygon components into persisted candidate sites.

    No area threshold is applied at this stage. Every valid polygon is retained
    as a preliminary candidate pending later land-use, slope, protection-area,
    grid-distance and minimum-contiguous-area checks.
    """

    database_path = RUNTIME_ROOT / "site_jobs.db"

    job_store = job_store or SQLiteJobStore(database_path)
    candidate_store = candidate_store or SQLiteCandidateStore(
        database_path
    )

    job = job_store.get(job_id)

    existing_evidence = next(
        (
            item
            for item in job.evidence
            if item.evidence_type == EvidenceType.CANDIDATE_SET
        ),
        None,
    )

    if (
        job.status == JobStatus.CANDIDATES_READY
        and existing_evidence is not None
        and _resolve_artifact_path(
            existing_evidence.artifact_path
        ).exists()
        and candidate_store.count_by_job(job_id) > 0
    ):
        return job

    if job.status != JobStatus.NON_WATER_READY:
        raise ValueError(
            "Candidate generation requires status non_water_ready; "
            f"received {job.status.value}."
        )

    non_water_evidence = next(
        (
            item
            for item in job.evidence
            if item.evidence_type == EvidenceType.NON_WATER_AREA
        ),
        None,
    )

    if non_water_evidence is None:
        raise ValueError(
            "The job has no non-water-area evidence."
        )

    source_path = _resolve_artifact_path(
        non_water_evidence.artifact_path
    )

    output_path = (
        RUNTIME_ROOT
        / "jobs"
        / job.job_id
        / "candidate_sites.geojson"
    )

    try:
        job.transition(JobStatus.BUILDING_CANDIDATES)

        job_store.save(
            job,
            event_type="candidate_generation_started",
            event_payload={
                "source_evidence_id": (
                    non_water_evidence.evidence_id
                ),
                "source_artifact": _relative_path(source_path),
                "filter_rule": "none",
                "ranking_rule": "area_descending",
            },
        )

        if not source_path.exists():
            raise FileNotFoundError(
                f"Non-water artifact not found: {source_path}"
            )

        polygons = gpd.read_file(source_path)

        if polygons.empty:
            raise ValueError(
                "The non-water artifact contains no polygon features."
            )

        if polygons.crs is None:
            polygons = polygons.set_crs("EPSG:4326")

        polygons = polygons[
            polygons.geometry.notna()
            & ~polygons.geometry.is_empty
        ].copy()

        polygons["geometry"] = polygons.geometry.make_valid()

        polygons = polygons.explode(
            index_parts=False,
            ignore_index=True,
        )

        polygons = polygons[
            polygons.geometry.geom_type.isin(
                ["Polygon", "MultiPolygon"]
            )
        ].copy()

        if polygons.empty:
            raise ValueError(
                "No valid polygon geometry remained after normalization."
            )

        polygons = polygons.to_crs("EPSG:4326")

        metric_crs = non_water_evidence.metadata.get(
            "metric_crs"
        )

        if not metric_crs:
            estimated_crs = polygons.estimate_utm_crs()

            if estimated_crs is None:
                raise ValueError(
                    "Unable to determine a projected CRS."
                )

            metric_crs = estimated_crs.to_string()

        polygons_metric = polygons.to_crs(metric_crs)

        areas_km2 = (
            polygons_metric.geometry.area / 1_000_000
        )

        centroids_wgs84 = gpd.GeoSeries(
            polygons_metric.geometry.centroid,
            crs=metric_crs,
        ).to_crs("EPSG:4326")

        records: list[dict] = []

        for index in range(len(polygons)):
            geometry = polygons.geometry.iloc[index]
            geometry_hash = _geometry_sha256(geometry)

            records.append(
                {
                    "geometry": geometry,
                    "geometry_sha256": geometry_hash,
                    "area_km2": float(areas_km2.iloc[index]),
                    "centroid_lon": float(
                        centroids_wgs84.iloc[index].x
                    ),
                    "centroid_lat": float(
                        centroids_wgs84.iloc[index].y
                    ),
                }
            )

        hashes = [
            record["geometry_sha256"]
            for record in records
        ]

        if len(hashes) != len(set(hashes)):
            raise ValueError(
                "Duplicate polygon geometries were found in "
                "the candidate source."
            )

        records.sort(
            key=lambda record: (
                -record["area_km2"],
                record["geometry_sha256"],
            )
        )

        artifact_path_value = _relative_path(output_path)

        candidates: list[CandidateSite] = []
        output_properties: list[dict] = []
        output_geometries: list[object] = []

        for rank, record in enumerate(records, start=1):
            candidate = CandidateSite(
                candidate_id=_candidate_id(
                    job_id=job.job_id,
                    geometry_sha256=(
                        record["geometry_sha256"]
                    ),
                ),
                job_id=job.job_id,
                rank=rank,
                area_km2=record["area_km2"],
                centroid_lon=record["centroid_lon"],
                centroid_lat=record["centroid_lat"],
                geometry_sha256=(
                    record["geometry_sha256"]
                ),
                artifact_path=artifact_path_value,
                source_evidence_id=(
                    non_water_evidence.evidence_id
                ),
            )

            candidates.append(candidate)

            output_properties.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "job_id": candidate.job_id,
                    "rank": candidate.rank,
                    "stage": candidate.stage.value,
                    "screening_status": (
                        candidate.screening_status.value
                    ),
                    "area_km2": candidate.area_km2,
                    "centroid_lon": candidate.centroid_lon,
                    "centroid_lat": candidate.centroid_lat,
                    "geometry_sha256": (
                        candidate.geometry_sha256
                    ),
                    "source_evidence_id": (
                        candidate.source_evidence_id
                    ),
                }
            )

            output_geometries.append(record["geometry"])

        candidate_area_km2 = sum(
            candidate.area_km2
            for candidate in candidates
        )

        expected_area_km2 = float(
            non_water_evidence.metadata["result_area_km2"]
        )

        conservation_error_km2 = abs(
            candidate_area_km2 - expected_area_km2
        )

        tolerance_km2 = max(
            expected_area_km2 * 1e-6,
            0.001,
        )

        if conservation_error_km2 > tolerance_km2:
            raise ValueError(
                "Candidate area validation failed: "
                f"{conservation_error_km2:.9f} km²."
            )

        candidate_frame = gpd.GeoDataFrame(
            output_properties,
            geometry=output_geometries,
            crs="EPSG:4326",
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = output_path.with_name(
            output_path.stem + ".tmp.geojson"
        )
        temporary_path.unlink(missing_ok=True)

        candidate_frame.to_file(
            temporary_path,
            driver="GeoJSON",
        )

        temporary_path.replace(output_path)

        persisted_count = candidate_store.replace_for_job(
            job.job_id,
            candidates,
        )

        if persisted_count != len(candidates):
            raise RuntimeError(
                "Candidate persistence count does not match "
                "the generated candidate count."
            )

        job.evidence = [
            item
            for item in job.evidence
            if item.evidence_type
            != EvidenceType.CANDIDATE_SET
        ]

        job.evidence.append(
            Evidence(
                evidence_type=EvidenceType.CANDIDATE_SET,
                provider="Derived GIS analysis",
                source_identifier=(
                    "candidate-generation:"
                    f"{non_water_evidence.evidence_id}"
                ),
                artifact_path=artifact_path_value,
                checksum_sha256=_sha256_file(output_path),
                source_license=(
                    "Derived from geoBoundaries CC BY 4.0 "
                    "and OpenStreetMap ODbL 1.0"
                ),
                metadata={
                    "source_evidence_id": (
                        non_water_evidence.evidence_id
                    ),
                    "candidate_count": len(candidates),
                    "total_area_km2": round(
                        candidate_area_km2,
                        6,
                    ),
                    "expected_area_km2": round(
                        expected_area_km2,
                        6,
                    ),
                    "area_conservation_error_km2": round(
                        conservation_error_km2,
                        9,
                    ),
                    "metric_crs": metric_crs,
                    "filter_rule": "none",
                    "ranking_rule": "area_descending",
                    "initial_screening_status": (
                        "not_evaluated"
                    ),
                },
            )
        )

        job.transition(JobStatus.CANDIDATES_READY)

        job_store.save(
            job,
            event_type="candidate_generation_completed",
            event_payload={
                "artifact_path": artifact_path_value,
                "candidate_count": len(candidates),
                "total_area_km2": round(
                    candidate_area_km2,
                    6,
                ),
                "area_conservation_error_km2": round(
                    conservation_error_km2,
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

        job_store.save(
            job,
            event_type="job_failed",
            event_payload={
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "retryable": False,
            },
        )

        raise

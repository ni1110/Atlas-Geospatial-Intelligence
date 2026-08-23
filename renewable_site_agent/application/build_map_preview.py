"""Build lightweight GeoJSON artifacts for interactive map rendering."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyogrio import set_gdal_config_options
from shapely import make_valid

from renewable_site_agent.application.create_job import (
    PROJECT_ROOT,
    RUNTIME_ROOT,
)
from renewable_site_agent.domain.evidence import (
    Evidence,
    EvidenceType,
)
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import (
    SQLiteJobStore,
)



def _local_sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MapPreviewError(RuntimeError):
    """Raised when map preview artifacts cannot be generated."""


@dataclass(frozen=True)
class MapPreviewResult:
    """Summary of generated lightweight map artifacts."""

    job_id: str
    preview_directory: Path
    boundary_path: Path
    water_path: Path
    candidate_path: Path
    boundary_feature_count: int
    source_water_feature_count: int
    preview_water_feature_count: int
    candidate_feature_count: int
    source_total_bytes: int
    preview_total_bytes: int
    reduction_ratio: float


def _resolve_artifact_path(value: str) -> Path:
    path = Path(value)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _find_evidence(
    evidence_items: list[Evidence],
    evidence_type: EvidenceType,
) -> Evidence:
    evidence = next(
        (
            item
            for item in evidence_items
            if item.evidence_type == evidence_type
        ),
        None,
    )

    if evidence is None:
        raise MapPreviewError(
            f"Missing required evidence: {evidence_type.value}"
        )

    return evidence


def _verify_source(evidence: Evidence) -> Path:
    path = _resolve_artifact_path(evidence.artifact_path)

    if not path.is_file():
        raise MapPreviewError(
            f"Source artifact does not exist: {path}"
        )

    actual_checksum = _sha256_file(path)

    if actual_checksum != evidence.checksum_sha256:
        raise MapPreviewError(
            "Source artifact checksum mismatch: "
            f"{evidence.evidence_type.value}"
        )

    return path


def _write_geojson_atomic(
    frame: gpd.GeoDataFrame,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = output_path.with_name(
        output_path.stem + ".tmp.geojson"
    )
    temporary_path.unlink(missing_ok=True)

    frame.to_crs("EPSG:4326").to_file(
        temporary_path,
        driver="GeoJSON",
    )

    temporary_path.replace(output_path)


def _simplify_frame(
    frame: gpd.GeoDataFrame,
    *,
    metric_crs: str,
    tolerance_m: float,
) -> gpd.GeoDataFrame:
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")

    metric = frame.to_crs(metric_crs).copy()

    metric = metric[
        metric.geometry.notna()
        & ~metric.geometry.is_empty
    ].copy()

    original_geometry = metric.geometry.copy()

    metric["geometry"] = metric.geometry.make_valid()
    metric["geometry"] = metric.geometry.simplify(
        tolerance_m,
        preserve_topology=True,
    )

    invalid_mask = (
        metric.geometry.isna()
        | metric.geometry.is_empty
    )

    if invalid_mask.any():
        metric.loc[invalid_mask, "geometry"] = (
            original_geometry.loc[invalid_mask]
        )

    metric["geometry"] = metric.geometry.apply(
        make_valid
    )

    return metric



def _fast_final_candidate_preview(
    candidates: gpd.GeoDataFrame,
    *,
    metric_crs: str,
    tolerance_m: float,
) -> gpd.GeoDataFrame:
    """Create a lightweight preview for complex final candidate polygons.

    The authoritative final GeoJSON may contain very detailed geometries.
    For browser rendering, we keep feature identity/count and simplify geometry.
    This preview is only for visualization; full-resolution artifacts remain
    authoritative.
    """

    frame = candidates.copy()

    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")

    # For final outputs, the source is normally WGS84. Simplifying directly in
    # geographic coordinates avoids an expensive full reprojection of a very
    # complex 74 MB GeoJSON before simplification.
    if frame.crs is not None and frame.crs.is_geographic:
        work = frame.copy()
        # Conservative degree approximation. 300 m is enough for browser preview
        # while keeping the visible footprint meaningful.
        simplify_tolerance = max(float(tolerance_m), 300.0) / 111_320.0
    else:
        work = frame.to_crs(metric_crs).copy()
        simplify_tolerance = max(float(tolerance_m), 300.0)

    original_geometry = work.geometry
    simplified_geometry = original_geometry.simplify(
        simplify_tolerance,
        preserve_topology=False,
    )

    # Keep feature count stable. Tiny polygons can collapse during simplification;
    # replace collapsed preview geometry with the original envelope.
    bad = simplified_geometry.isna() | simplified_geometry.is_empty
    if bad.any():
        simplified_geometry.loc[bad] = original_geometry.loc[bad].envelope

    work.geometry = simplified_geometry

    # Browser map expects lon/lat GeoJSON.
    if work.crs is None:
        work = work.set_crs("EPSG:4326")

    return work.to_crs("EPSG:4326")


def build_map_preview(
    job_id: str,
    *,
    tolerance_m: float = 75.0,
    store: SQLiteJobStore | None = None,
) -> MapPreviewResult:
    """
    Build lightweight display artifacts without changing analysis evidence.

    Full-resolution GeoJSON remains the source of truth for computation,
    validation, reporting and audit.
    """

    if tolerance_m <= 0:
        raise ValueError("tolerance_m must be greater than zero.")

    store = store or SQLiteJobStore(
        RUNTIME_ROOT / "site_jobs.db"
    )

    job = store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise MapPreviewError(
            "Map preview requires status candidates_ready; "
            f"received {job.status.value}."
        )

    boundary_evidence = _find_evidence(
        job.evidence,
        EvidenceType.ADMINISTRATIVE_BOUNDARY,
    )
    water_evidence = _find_evidence(
        job.evidence,
        EvidenceType.WATER_EXCLUSION,
    )
    candidate_evidence = _find_evidence(
        job.evidence,
        EvidenceType.CANDIDATE_SET,
    )

    boundary_source = _verify_source(boundary_evidence)
    water_source = _verify_source(water_evidence)
    candidate_source = _verify_source(candidate_evidence)

    candidate_source_stage = "preliminary_non_water"
    candidate_screening_status = "not_evaluated"
    candidate_source_kind = "candidate_evidence"

    final_candidate_source = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "final"
        / "available_data_screened_candidate_sites.geojson"
    )
    final_decision_log = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "final"
        / "screening_decision_log.json"
    )

    if final_candidate_source.is_file() and final_decision_log.is_file():
        decision_log = json.loads(
            final_decision_log.read_text(encoding="utf-8")
        )
        expected_final_sha256 = decision_log["latest_candidate_output"]["sha256"]

        if _local_sha256_file(final_candidate_source) != expected_final_sha256:
            raise MapPreviewError(
                "Final candidate checksum does not match decision log."
            )

        candidate_source = final_candidate_source
        candidate_source_stage = "available_data_screened_preliminary_candidates"
        candidate_screening_status = decision_log.get(
            "result_status",
            "preliminary_not_final_recommendation",
        )
        candidate_source_kind = "available_data_final"

    metric_crs = candidate_evidence.metadata.get(
        "metric_crs"
    )

    if not metric_crs:
        raise MapPreviewError(
            "Candidate evidence does not contain metric_crs."
        )

    preview_directory = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "map_preview"
    )

    boundary_output = (
        preview_directory / "boundary_preview.geojson"
    )
    water_output = (
        preview_directory / "water_preview.geojson"
    )
    candidate_output = (
        preview_directory / "candidate_preview.geojson"
    )
    metadata_output = (
        preview_directory / "preview_metadata.json"
    )

    # Some verified local GeoJSON artifacts contain large dissolved geometries.
    # Allow GDAL/pyogrio to read them during preview generation.
    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})

    boundary = gpd.read_file(boundary_source)
    water = gpd.read_file(water_source)
    candidates = gpd.read_file(candidate_source)

    boundary_preview = _simplify_frame(
        boundary,
        metric_crs=metric_crs,
        tolerance_m=tolerance_m,
    )

    boundary_columns = [
        column
        for column in [
            "region",
            "display_name",
            "source_provider",
            "source_identifier",
            "source_license",
            "geometry",
        ]
        if column in boundary_preview.columns
    ]
    boundary_preview = boundary_preview[
        boundary_columns
    ].copy()

    # Water is a visual exclusion mask. Dissolving thousands of source
    # features drastically reduces browser-side feature creation overhead.
    water_metric = _simplify_frame(
        water,
        metric_crs=metric_crs,
        tolerance_m=tolerance_m,
    )

    water_union = make_valid(
        water_metric.geometry.union_all()
    )

    water_preview = gpd.GeoDataFrame(
        {
            "layer_role": ["excluded_water"],
            "source_feature_count": [len(water)],
            "source_provider": [
                "Geofabrik / OpenStreetMap"
            ],
            "source_license": ["ODbL 1.0"],
            "source_attribution": [
                "© OpenStreetMap contributors"
            ],
        },
        geometry=[water_union],
        crs=metric_crs,
    )

    if candidate_source_kind == "available_data_final":
        candidate_preview = _fast_final_candidate_preview(
            candidates,
            metric_crs=metric_crs,
            tolerance_m=tolerance_m,
        )
    else:
        candidate_preview = _simplify_frame(
            candidates,
            metric_crs=metric_crs,
            tolerance_m=tolerance_m,
        )

    candidate_columns = [
        column
        for column in [
            "candidate_id",
            "job_id",
            "rank",
            "stage",
            "screening_status",
            "area_km2",
            "centroid_lon",
            "centroid_lat",
            "geometry_sha256",
            "grid_distance_status",
            "prime_farmland_status",
            "geometry",
        ]
        if column in candidate_preview.columns
    ]

    candidate_preview = candidate_preview[
        candidate_columns
    ].copy()

    if len(candidate_preview) != len(candidates):
        raise MapPreviewError(
            "Candidate preview feature count changed: "
            f"{len(candidate_preview)} != {len(candidates)}"
        )

    _write_geojson_atomic(
        boundary_preview,
        boundary_output,
    )
    _write_geojson_atomic(
        water_preview,
        water_output,
    )
    _write_geojson_atomic(
        candidate_preview,
        candidate_output,
    )

    source_paths = [
        boundary_source,
        water_source,
        candidate_source,
    ]
    preview_paths = [
        boundary_output,
        water_output,
        candidate_output,
    ]

    source_total_bytes = sum(
        path.stat().st_size for path in source_paths
    )
    preview_total_bytes = sum(
        path.stat().st_size for path in preview_paths
    )

    reduction_ratio = (
        1.0 - preview_total_bytes / source_total_bytes
        if source_total_bytes
        else 0.0
    )

    metadata: dict[str, Any] = {
        "job_id": job_id,
        "tolerance_m": tolerance_m,
        "candidate_source_kind": candidate_source_kind,
        "candidate_source_stage": candidate_source_stage,
        "candidate_screening_status": candidate_screening_status,
        "metric_crs": metric_crs,
        "source_artifacts": {
            "boundary": {
                "path": str(
                    boundary_source.relative_to(PROJECT_ROOT)
                ),
                "sha256": boundary_evidence.checksum_sha256,
                "feature_count": len(boundary),
            },
            "water": {
                "path": str(
                    water_source.relative_to(PROJECT_ROOT)
                ),
                "sha256": water_evidence.checksum_sha256,
                "feature_count": len(water),
            },
            "candidates": {
                "path": str(
                    candidate_source.relative_to(PROJECT_ROOT)
                ),
                "sha256": candidate_evidence.checksum_sha256,
                "feature_count": len(candidates),
            },
        },
        "preview_artifacts": {
            "boundary": {
                "path": str(
                    boundary_output.relative_to(PROJECT_ROOT)
                ),
                "sha256": _sha256_file(boundary_output),
                "feature_count": len(boundary_preview),
            },
            "water": {
                "path": str(
                    water_output.relative_to(PROJECT_ROOT)
                ),
                "sha256": _sha256_file(water_output),
                "feature_count": len(water_preview),
                "source_feature_count": len(water),
            },
            "candidates": {
                "path": str(
                    candidate_output.relative_to(PROJECT_ROOT)
                ),
                "sha256": _sha256_file(candidate_output),
                "feature_count": len(candidate_preview),
            },
        },
        "source_total_bytes": source_total_bytes,
        "preview_total_bytes": preview_total_bytes,
        "reduction_ratio": reduction_ratio,
        "warning": (
            "Preview artifacts are for visualization only. "
            "Full-resolution evidence remains authoritative."
        ),
    }

    temporary_metadata = metadata_output.with_suffix(
        ".tmp.json"
    )
    temporary_metadata.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_metadata.replace(metadata_output)

    return MapPreviewResult(
        job_id=job_id,
        preview_directory=preview_directory,
        boundary_path=boundary_output,
        water_path=water_output,
        candidate_path=candidate_output,
        boundary_feature_count=len(boundary_preview),
        source_water_feature_count=len(water),
        preview_water_feature_count=len(water_preview),
        candidate_feature_count=len(candidate_preview),
        source_total_bytes=source_total_bytes,
        preview_total_bytes=preview_total_bytes,
        reduction_ratio=reduction_ratio,
    )

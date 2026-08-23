"""Apply slope suitability polygons to preliminary non-water candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely import make_valid
from pyogrio import set_gdal_config_options

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.evidence import Evidence, EvidenceType
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.candidate_store import SQLiteCandidateStore
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


OUTPUT_STAGE = "preliminary_non_water_slope_screened"
OUTPUT_STATUS = "not_evaluated_for_other_constraints"


class SlopeScreeningError(RuntimeError):
    """Raised when slope screening cannot be completed."""


@dataclass(frozen=True)
class SlopeScreeningResult:
    job_id: str
    output_path: Path
    metadata_path: Path
    output_sha256: str
    input_candidate_count: int
    output_candidate_count: int
    output_area_km2: float
    removed_by_slope_area_km2: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def geometry_sha256(geometry: Any) -> str:
    return hashlib.sha256(geometry.wkb).hexdigest()


def relative_to_project(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def write_geojson_atomic(frame: gpd.GeoDataFrame, output_path: Path) -> None:
    temporary_path = output_path.with_name(output_path.stem + ".tmp.geojson")
    temporary_path.unlink(missing_ok=True)
    frame.to_file(temporary_path, driver="GeoJSON")
    temporary_path.replace(output_path)


def find_evidence(
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
        raise SlopeScreeningError(
            f"Missing required evidence: {evidence_type.value}"
        )

    return evidence


def resolve_verified_artifact(evidence: Evidence) -> Path:
    path = Path(evidence.artifact_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    path = path.resolve()

    if not path.is_file():
        raise SlopeScreeningError(f"Artifact does not exist: {path}")

    if sha256_file(path) != evidence.checksum_sha256:
        raise SlopeScreeningError(
            "Artifact checksum mismatch: "
            f"{evidence.evidence_type.value}"
        )

    return path


def ensure_crs(frame: gpd.GeoDataFrame, fallback_crs: str) -> gpd.GeoDataFrame:
    if frame.crs is not None:
        return frame

    bounds = frame.total_bounds
    west, south, east, north = bounds

    looks_like_lonlat = (
        -180 <= west <= 180
        and -90 <= south <= 90
        and -180 <= east <= 180
        and -90 <= north <= 90
    )

    if looks_like_lonlat:
        return frame.set_crs("EPSG:4326")

    return frame.set_crs(fallback_crs)


def make_output_candidate_id(
    job_id: str,
    source_candidate_id: str,
    slope_sha256: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{job_id}:{OUTPUT_STAGE}:{source_candidate_id}:{slope_sha256}",
        )
    )


def apply_slope_screening(job_id: str) -> SlopeScreeningResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    candidate_store = SQLiteCandidateStore(database_path)

    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise SlopeScreeningError(
            "Slope screening requires candidates_ready; "
            f"received {job.status.value}."
        )

    candidate_evidence = find_evidence(
        job.evidence,
        EvidenceType.CANDIDATE_SET,
    )

    candidate_path = resolve_verified_artifact(candidate_evidence)

    expected_candidate_count = int(
        candidate_evidence.metadata["candidate_count"]
    )
    database_candidate_count = candidate_store.count_by_job(job_id)

    if database_candidate_count != expected_candidate_count:
        raise SlopeScreeningError(
            "Candidate database count does not match evidence metadata: "
            f"{database_candidate_count} != {expected_candidate_count}"
        )

    metric_crs = candidate_evidence.metadata.get("metric_crs")

    if not metric_crs:
        raise SlopeScreeningError(
            "Candidate evidence does not contain metric_crs."
        )

    terrain_dir = RUNTIME_ROOT / "jobs" / job_id / "terrain"
    slope_vector_metadata_path = terrain_dir / "slope_vector_metadata.json"

    if not slope_vector_metadata_path.is_file():
        raise SlopeScreeningError(
            f"Missing slope vector metadata: {slope_vector_metadata_path}"
        )

    slope_vector_metadata = json.loads(
        slope_vector_metadata_path.read_text(encoding="utf-8")
    )

    slope_area_path = PROJECT_ROOT / slope_vector_metadata["output"]["path"]
    slope_area_sha256 = slope_vector_metadata["output"]["sha256"]

    if not slope_area_path.is_file():
        raise SlopeScreeningError(
            f"Missing slope suitable area: {slope_area_path}"
        )

    if sha256_file(slope_area_path) != slope_area_sha256:
        raise SlopeScreeningError(
            "Slope suitable area checksum does not match metadata."
        )

    candidates = gpd.read_file(candidate_path)
    candidates = ensure_crs(candidates, metric_crs)

    # The vectorized slope-suitable GeoJSON can contain one very large
    # dissolved polygon. GDAL protects against very large GeoJSON objects by
    # default, so we explicitly allow this verified local artifact.
    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})

    slope_area = gpd.read_file(slope_area_path)
    slope_area = ensure_crs(
        slope_area,
        str(slope_vector_metadata["output"]["crs"]),
    )

    if len(candidates) != expected_candidate_count:
        raise SlopeScreeningError(
            "Candidate GeoJSON count does not match evidence metadata: "
            f"{len(candidates)} != {expected_candidate_count}"
        )

    candidates_metric = candidates.to_crs(metric_crs).copy()
    slope_metric = slope_area.to_crs(metric_crs).copy()

    candidates_metric = candidates_metric[
        candidates_metric.geometry.notna()
        & ~candidates_metric.geometry.is_empty
    ].copy()

    candidates_metric["geometry"] = candidates_metric.geometry.apply(make_valid)
    slope_metric["geometry"] = slope_metric.geometry.apply(make_valid)

    suitable_union = make_valid(slope_metric.geometry.union_all())

    screened = candidates_metric.copy()
    screened["source_candidate_id"] = screened["candidate_id"]
    screened["source_candidate_rank"] = screened["rank"]
    screened["geometry"] = screened.geometry.intersection(suitable_union)

    screened = screened[
        screened.geometry.notna()
        & ~screened.geometry.is_empty
    ].copy()

    if screened.empty:
        raise SlopeScreeningError(
            "Slope screening removed all candidate geometries."
        )

    screened["geometry"] = screened.geometry.apply(make_valid)

    screened["area_km2"] = screened.geometry.area / 1_000_000.0

    screened = screened[screened["area_km2"] > 0].copy()

    if screened.empty:
        raise SlopeScreeningError(
            "Slope screening produced only zero-area geometries."
        )

    screened = screened.sort_values(
        ["area_km2", "source_candidate_rank"],
        ascending=[False, True],
    ).reset_index(drop=True)

    screened["rank"] = range(1, len(screened) + 1)

    screened["candidate_id"] = [
        make_output_candidate_id(
            job_id,
            str(source_candidate_id),
            slope_area_sha256,
        )
        for source_candidate_id in screened["source_candidate_id"]
    ]

    screened["stage"] = OUTPUT_STAGE
    screened["screening_status"] = OUTPUT_STATUS
    screened["slope_screening_status"] = "passed"
    screened["max_slope_deg"] = slope_vector_metadata["method"]["max_slope_deg"]
    screened["slope_area_sha256"] = slope_area_sha256

    screened_wgs84 = screened.to_crs("EPSG:4326").copy()

    centroids = screened.geometry.centroid
    centroids_wgs84 = gpd.GeoSeries(
        centroids,
        crs=metric_crs,
    ).to_crs("EPSG:4326")

    screened_wgs84["centroid_lon"] = centroids_wgs84.x
    screened_wgs84["centroid_lat"] = centroids_wgs84.y

    screened_wgs84["geometry_sha256"] = [
        geometry_sha256(geometry)
        for geometry in screened.geometry
    ]

    keep_columns = [
        "candidate_id",
        "job_id",
        "rank",
        "stage",
        "screening_status",
        "slope_screening_status",
        "max_slope_deg",
        "area_km2",
        "centroid_lon",
        "centroid_lat",
        "source_candidate_id",
        "source_candidate_rank",
        "geometry_sha256",
        "slope_area_sha256",
        "geometry",
    ]

    output = screened_wgs84[
        [column for column in keep_columns if column in screened_wgs84.columns]
    ].copy()

    output_path = terrain_dir / "slope_screened_candidate_sites.geojson"
    metadata_path = terrain_dir / "slope_screening_metadata.json"

    write_geojson_atomic(output, output_path)

    output_area_km2 = float(screened["area_km2"].sum())
    input_area_km2 = float(candidate_evidence.metadata["total_area_km2"])
    removed_by_slope_area_km2 = input_area_km2 - output_area_km2

    metadata = {
        "job_id": job_id,
        "stage": "slope_screening",
        "created_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "input_candidates": {
            "path": relative_to_project(candidate_path),
            "sha256": candidate_evidence.checksum_sha256,
            "feature_count": expected_candidate_count,
            "database_candidate_count": database_candidate_count,
            "total_area_km2": input_area_km2,
            "stage": candidate_evidence.metadata.get("initial_screening_status"),
        },
        "input_slope_suitable_area": {
            "path": relative_to_project(slope_area_path),
            "sha256": slope_area_sha256,
            "suitable_area_km2": slope_vector_metadata["output"]["suitable_area_km2"],
            "max_slope_deg": slope_vector_metadata["method"]["max_slope_deg"],
        },
        "method": {
            "operation": "intersection",
            "description": (
                "Preliminary non-water candidates are intersected with the "
                "slope-suitable area derived from Copernicus DEM GLO-30."
            ),
            "metric_crs": metric_crs,
            "screening_rule": "candidate_area ∩ slope_degrees < max_slope_deg",
            "output_stage": OUTPUT_STAGE,
            "output_status": OUTPUT_STATUS,
        },
        "output": {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "feature_count": int(len(output)),
            "total_area_km2": output_area_km2,
            "removed_by_slope_area_km2": removed_by_slope_area_km2,
            "area_retention_ratio": output_area_km2 / input_area_km2,
            "size_bytes": output_path.stat().st_size,
        },
        "warning": (
            "This output satisfies water exclusion and preliminary slope screening only. "
            "Protected areas, land use, farmland, grid distance and other business rules "
            "have not yet been evaluated."
        ),
    }

    write_json_atomic(metadata_path, metadata)

    return SlopeScreeningResult(
        job_id=job_id,
        output_path=output_path,
        metadata_path=metadata_path,
        output_sha256=metadata["output"]["sha256"],
        input_candidate_count=expected_candidate_count,
        output_candidate_count=int(len(output)),
        output_area_km2=output_area_km2,
        removed_by_slope_area_km2=removed_by_slope_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = apply_slope_screening(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "output_path": str(result.output_path),
                "metadata_path": str(result.metadata_path),
                "output_sha256": result.output_sha256,
                "input_candidate_count": result.input_candidate_count,
                "output_candidate_count": result.output_candidate_count,
                "output_area_km2": round(result.output_area_km2, 6),
                "removed_by_slope_area_km2": round(
                    result.removed_by_slope_area_km2,
                    6,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

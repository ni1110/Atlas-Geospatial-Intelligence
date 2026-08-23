"""Apply protected-area exclusion to slope-screened candidate sites."""

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
from pyogrio import set_gdal_config_options
from shapely import make_valid

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


OUTPUT_STAGE = "preliminary_non_water_slope_protected_screened"
OUTPUT_STATUS = "not_evaluated_for_land_use_grid"


class ProtectedAreaScreeningError(RuntimeError):
    """Raised when protected-area screening cannot be completed."""


@dataclass(frozen=True)
class ProtectedAreaScreeningResult:
    job_id: str
    output_path: Path
    metadata_path: Path
    output_sha256: str
    input_candidate_count: int
    output_candidate_count: int
    output_area_km2: float
    removed_by_protected_area_km2: float


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def write_geojson_atomic(frame: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(output_path.stem + ".tmp.geojson")
    temporary_path.unlink(missing_ok=True)
    frame.to_file(temporary_path, driver="GeoJSON")
    temporary_path.replace(output_path)


def make_output_candidate_id(
    job_id: str,
    source_candidate_id: str,
    protected_area_sha256: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{job_id}:{OUTPUT_STAGE}:{source_candidate_id}:{protected_area_sha256}",
        )
    )


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProtectedAreaScreeningError(f"Missing metadata file: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project_path(relative_or_absolute: str) -> Path:
    path = Path(relative_or_absolute)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def apply_protected_area_screening(job_id: str) -> ProtectedAreaScreeningResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise ProtectedAreaScreeningError(
            "Protected-area screening requires candidates_ready; "
            f"received {job.status.value}."
        )

    terrain_dir = RUNTIME_ROOT / "jobs" / job_id / "terrain"
    protected_dir = RUNTIME_ROOT / "jobs" / job_id / "protected_areas"

    slope_screening_metadata_path = terrain_dir / "slope_screening_metadata.json"
    protected_metadata_path = protected_dir / "protected_areas_metadata.json"

    slope_metadata = read_json(slope_screening_metadata_path)
    protected_metadata = read_json(protected_metadata_path)

    input_candidate_path = resolve_project_path(slope_metadata["output"]["path"])
    protected_area_path = resolve_project_path(protected_metadata["output"]["path"])

    expected_candidate_sha256 = slope_metadata["output"]["sha256"]
    expected_protected_sha256 = protected_metadata["output"]["sha256"]

    if not input_candidate_path.is_file():
        raise ProtectedAreaScreeningError(
            f"Missing slope-screened candidates: {input_candidate_path}"
        )

    if not protected_area_path.is_file():
        raise ProtectedAreaScreeningError(
            f"Missing protected-area exclusion file: {protected_area_path}"
        )

    if sha256_file(input_candidate_path) != expected_candidate_sha256:
        raise ProtectedAreaScreeningError(
            "Slope-screened candidate checksum does not match metadata."
        )

    if sha256_file(protected_area_path) != expected_protected_sha256:
        raise ProtectedAreaScreeningError(
            "Protected-area exclusion checksum does not match metadata."
        )

    metric_crs = slope_metadata["method"]["metric_crs"]

    # Allow verified local GeoJSON artifacts with large dissolved geometries.
    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})

    candidates = gpd.read_file(input_candidate_path)
    protected = gpd.read_file(protected_area_path)

    if candidates.crs is None:
        candidates = candidates.set_crs("EPSG:4326")

    if protected.crs is None:
        protected = protected.set_crs("EPSG:4326")

    input_candidate_count = len(candidates)
    expected_input_count = int(slope_metadata["output"]["feature_count"])

    if input_candidate_count != expected_input_count:
        raise ProtectedAreaScreeningError(
            "Slope-screened candidate count does not match metadata: "
            f"{input_candidate_count} != {expected_input_count}"
        )

    candidates_metric = candidates.to_crs(metric_crs).copy()
    protected_metric = protected.to_crs(metric_crs).copy()

    candidates_metric = candidates_metric[
        candidates_metric.geometry.notna()
        & ~candidates_metric.geometry.is_empty
    ].copy()

    protected_metric = protected_metric[
        protected_metric.geometry.notna()
        & ~protected_metric.geometry.is_empty
    ].copy()

    if protected_metric.empty:
        raise ProtectedAreaScreeningError(
            "Protected-area exclusion layer is empty."
        )

    candidates_metric["geometry"] = candidates_metric.geometry.apply(make_valid)
    protected_metric["geometry"] = protected_metric.geometry.apply(make_valid)

    protected_union = make_valid(protected_metric.geometry.union_all())

    screened = candidates_metric.copy()
    screened["source_candidate_id"] = screened["candidate_id"]
    screened["source_candidate_rank"] = screened["rank"]
    screened["geometry"] = screened.geometry.difference(protected_union)

    screened = screened[
        screened.geometry.notna()
        & ~screened.geometry.is_empty
    ].copy()

    if screened.empty:
        raise ProtectedAreaScreeningError(
            "Protected-area screening removed all candidate geometries."
        )

    screened["geometry"] = screened.geometry.apply(make_valid)
    screened["area_km2"] = screened.geometry.area / 1_000_000.0
    screened = screened[screened["area_km2"] > 0].copy()

    if screened.empty:
        raise ProtectedAreaScreeningError(
            "Protected-area screening produced only zero-area geometries."
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
            expected_protected_sha256,
        )
        for source_candidate_id in screened["source_candidate_id"]
    ]

    screened["stage"] = OUTPUT_STAGE
    screened["screening_status"] = OUTPUT_STATUS
    screened["protected_area_screening_status"] = "passed"
    screened["protected_area_sha256"] = expected_protected_sha256

    centroids_metric = screened.geometry.centroid
    centroids_wgs84 = gpd.GeoSeries(
        centroids_metric,
        crs=metric_crs,
    ).to_crs("EPSG:4326")

    screened["centroid_lon"] = centroids_wgs84.x
    screened["centroid_lat"] = centroids_wgs84.y
    screened["geometry_sha256"] = [
        geometry_sha256(geometry)
        for geometry in screened.geometry
    ]

    output_wgs84 = screened.to_crs("EPSG:4326").copy()

    keep_columns = [
        "candidate_id",
        "job_id",
        "rank",
        "stage",
        "screening_status",
        "protected_area_screening_status",
        "max_slope_deg",
        "area_km2",
        "centroid_lon",
        "centroid_lat",
        "source_candidate_id",
        "source_candidate_rank",
        "geometry_sha256",
        "slope_area_sha256",
        "protected_area_sha256",
        "geometry",
    ]

    output = output_wgs84[
        [column for column in keep_columns if column in output_wgs84.columns]
    ].copy()

    output_path = protected_dir / "protected_area_screened_candidate_sites.geojson"
    metadata_path = protected_dir / "protected_area_screening_metadata.json"

    write_geojson_atomic(output, output_path)

    input_area_km2 = float(slope_metadata["output"]["total_area_km2"])
    output_area_km2 = float(screened["area_km2"].sum())
    removed_by_protected_area_km2 = input_area_km2 - output_area_km2

    metadata = {
        "job_id": job_id,
        "stage": "protected_area_screening",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_candidates": {
            "path": relative_to_project(input_candidate_path),
            "sha256": expected_candidate_sha256,
            "feature_count": input_candidate_count,
            "total_area_km2": input_area_km2,
            "stage": slope_metadata["method"]["output_stage"],
        },
        "input_protected_areas": {
            "path": relative_to_project(protected_area_path),
            "sha256": expected_protected_sha256,
            "union_area_km2": protected_metadata["output"]["union_area_km2"],
            "source_feature_count": protected_metadata["source"]["source_feature_count"],
            "clipped_source_feature_count": protected_metadata["output"]["clipped_source_feature_count"],
            "source_license": protected_metadata["source"]["source_license"],
            "source_attribution": protected_metadata["source"]["source_attribution"],
        },
        "method": {
            "operation": "difference",
            "metric_crs": metric_crs,
            "description": (
                "Slope-screened candidates are subtracted by the dissolved "
                "protected-area exclusion layer from the local Beijing OSM GeoPackage."
            ),
            "screening_rule": "candidate_area - protected_area_exclusion",
            "output_stage": OUTPUT_STAGE,
            "output_status": OUTPUT_STATUS,
        },
        "output": {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "feature_count": int(len(output)),
            "total_area_km2": output_area_km2,
            "removed_by_protected_area_km2": removed_by_protected_area_km2,
            "area_retention_ratio": output_area_km2 / input_area_km2,
            "size_bytes": output_path.stat().st_size,
        },
        "warning": (
            "This output satisfies water exclusion, slope screening, and OSM protected-area "
            "exclusion only. Land use, farmland, grid distance and other business rules "
            "have not yet been evaluated. OSM protected-area data should be cross-checked "
            "against official registers before final decisions."
        ),
    }

    write_json_atomic(metadata_path, metadata)

    return ProtectedAreaScreeningResult(
        job_id=job_id,
        output_path=output_path,
        metadata_path=metadata_path,
        output_sha256=metadata["output"]["sha256"],
        input_candidate_count=input_candidate_count,
        output_candidate_count=int(len(output)),
        output_area_km2=output_area_km2,
        removed_by_protected_area_km2=removed_by_protected_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = apply_protected_area_screening(args.job_id)

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
                "removed_by_protected_area_km2": round(
                    result.removed_by_protected_area_km2,
                    6,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

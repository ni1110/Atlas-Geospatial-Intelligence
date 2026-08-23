"""Validate available-data GeoAgent screening outputs for one job."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyogrio import set_gdal_config_options

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"
METRIC_CRS = "EPSG:32650"


class ValidationError(RuntimeError):
    """Raised when validation fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def read_geojson(path: Path) -> gpd.GeoDataFrame:
    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})
    frame = gpd.read_file(path)
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame


def union_area_overlap_km2(
    left: gpd.GeoDataFrame,
    right: gpd.GeoDataFrame,
) -> float:
    left_metric = left.to_crs(METRIC_CRS)
    right_metric = right.to_crs(METRIC_CRS)

    right_union = right_metric.geometry.union_all()
    intersections = left_metric.geometry.intersection(right_union)

    return float(intersections.area.sum() / 1_000_000.0)


def validate_job(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    final_dir = job_dir / "final"
    map_preview_dir = job_dir / "map_preview"
    protected_dir = job_dir / "protected_areas"

    decision_log_path = final_dir / "screening_decision_log.json"
    report_path = final_dir / "available_data_screening_report.html"
    preview_metadata_path = map_preview_dir / "preview_metadata.json"
    preview_candidate_path = map_preview_dir / "candidate_preview.geojson"
    protected_metadata_path = protected_dir / "protected_areas_metadata.json"

    decision_log = read_json(decision_log_path)
    preview_metadata = read_json(preview_metadata_path)
    protected_metadata = read_json(protected_metadata_path)

    final_summary = decision_log["latest_candidate_output"]
    final_candidate_path = resolve_project_path(final_summary["path"])

    require(final_candidate_path.is_file(), f"Missing final candidates: {final_candidate_path}")
    require(report_path.is_file(), f"Missing report HTML: {report_path}")
    require(preview_candidate_path.is_file(), f"Missing preview candidates: {preview_candidate_path}")

    actual_final_sha256 = sha256_file(final_candidate_path)
    require(
        actual_final_sha256 == final_summary["sha256"],
        "Final candidate SHA-256 does not match decision log.",
    )

    final_candidates = read_geojson(final_candidate_path)
    preview_geojson = read_json(preview_candidate_path)

    final_count = int(len(final_candidates))
    preview_count = int(len(preview_geojson.get("features", [])))

    require(final_count == 608, f"Expected 608 final candidates, got {final_count}.")
    require(preview_count == 608, f"Expected 608 preview candidates, got {preview_count}.")

    require(
        preview_metadata.get("candidate_source_kind") == "available_data_final",
        "Map preview is not using available_data_final candidates.",
    )
    require(
        preview_metadata.get("candidate_source_stage")
        == "available_data_screened_preliminary_candidates",
        "Map preview candidate_source_stage is wrong.",
    )
    require(
        preview_metadata.get("candidate_screening_status")
        == "preliminary_not_final_recommendation",
        "Map preview candidate_screening_status is wrong.",
    )

    skipped = {
        item["constraint"]: item["status"]
        for item in decision_log.get("skipped_constraints", [])
    }

    require(
        skipped.get("max_distance_to_substation_km") == "skipped_not_evaluated",
        "Missing skipped max_distance_to_substation_km record.",
    )
    require(
        skipped.get("exclude_prime_farmland") == "skipped_not_evaluated",
        "Missing skipped exclude_prime_farmland record.",
    )

    html = report_path.read_text(encoding="utf-8")
    require(
        "max_distance_to_substation_km" in html
        and "skipped_not_evaluated" in html,
        "Report does not include substation skipped record.",
    )
    require(
        "exclude_prime_farmland" in html
        and "prime farmland" in html,
        "Report does not include prime farmland skipped record.",
    )

    water_path = job_dir / "water_exclusion.geojson"
    protected_path = resolve_project_path(protected_metadata["output"]["path"])

    require(water_path.is_file(), f"Missing water exclusion file: {water_path}")
    require(protected_path.is_file(), f"Missing protected-area file: {protected_path}")

    water = read_geojson(water_path)
    protected = read_geojson(protected_path)

    water_overlap_km2 = union_area_overlap_km2(final_candidates, water)
    protected_overlap_km2 = union_area_overlap_km2(final_candidates, protected)

    # Tiny tolerance for floating-point / topology artifacts.
    tolerance_km2 = 0.01

    require(
        water_overlap_km2 <= tolerance_km2,
        f"Final candidates overlap water by {water_overlap_km2:.6f} km².",
    )
    require(
        protected_overlap_km2 <= tolerance_km2,
        f"Final candidates overlap protected areas by {protected_overlap_km2:.6f} km².",
    )

    total_area_km2 = float(final_candidates["area_km2"].sum())

    require(
        abs(total_area_km2 - float(final_summary["total_area_km2"])) < 0.001,
        "Final candidate area does not match decision log.",
    )

    return {
        "job_id": job_id,
        "validation_status": "passed",
        "final_candidate_count": final_count,
        "preview_candidate_count": preview_count,
        "total_area_km2": round(total_area_km2, 6),
        "water_overlap_km2": round(water_overlap_km2, 9),
        "protected_overlap_km2": round(protected_overlap_km2, 9),
        "preview_candidate_source_kind": preview_metadata.get("candidate_source_kind"),
        "preview_candidate_source_stage": preview_metadata.get("candidate_source_stage"),
        "preview_candidate_screening_status": preview_metadata.get("candidate_screening_status"),
        "skipped_constraints": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_job(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Finalize available-data screened candidates and record skipped constraints."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyogrio import set_gdal_config_options

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


FINAL_STAGE = "available_data_screened_preliminary_candidates"
FINAL_STATUS = "preliminary_not_final_recommendation"


@dataclass(frozen=True)
class FinalizationResult:
    job_id: str
    output_path: Path
    decision_log_path: Path
    output_sha256: str
    candidate_count: int
    total_area_km2: float


class FinalizationError(RuntimeError):
    """Raised when finalization fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def relative_to_project(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FinalizationError(f"Missing metadata file: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


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


def finalize_available_data_candidates(job_id: str) -> FinalizationResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise FinalizationError(
            "Finalization requires candidates_ready; "
            f"received {job.status.value}."
        )

    protected_dir = RUNTIME_ROOT / "jobs" / job_id / "protected_areas"
    final_dir = RUNTIME_ROOT / "jobs" / job_id / "final"
    final_dir.mkdir(parents=True, exist_ok=True)

    protected_screening_metadata_path = (
        protected_dir / "protected_area_screening_metadata.json"
    )
    protected_metadata = read_json(protected_screening_metadata_path)

    latest_candidate_path = resolve_project_path(
        protected_metadata["output"]["path"]
    )
    expected_sha256 = protected_metadata["output"]["sha256"]

    if not latest_candidate_path.is_file():
        raise FinalizationError(
            f"Missing latest candidate file: {latest_candidate_path}"
        )

    if sha256_file(latest_candidate_path) != expected_sha256:
        raise FinalizationError(
            "Latest candidate checksum does not match metadata."
        )

    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})

    candidates = gpd.read_file(latest_candidate_path)

    if candidates.empty:
        raise FinalizationError("Latest candidate set is empty.")

    candidates = candidates.copy()
    candidates["stage"] = FINAL_STAGE
    candidates["screening_status"] = FINAL_STATUS
    candidates["grid_distance_status"] = "skipped_not_evaluated"
    candidates["grid_distance_reason"] = (
        "Local Beijing GeoPackage does not contain reliable power=substation "
        "or substation geometry data; user explicitly chose to skip this constraint."
    )
    candidates["prime_farmland_status"] = "skipped_not_evaluated"
    candidates["prime_farmland_reason"] = (
        "The current local data workflow does not include a reliable prime farmland "
        "or land-use restriction layer; this constraint was recorded as skipped."
    )

    output_path = final_dir / "available_data_screened_candidate_sites.geojson"
    decision_log_path = final_dir / "screening_decision_log.json"

    write_geojson_atomic(candidates, output_path)

    candidate_count = len(candidates)
    total_area_km2 = float(candidates["area_km2"].sum())

    decision_log = {
        "job_id": job_id,
        "stage": "available_data_candidate_finalization",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "result_status": FINAL_STATUS,
        "latest_candidate_output": {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "candidate_count": candidate_count,
            "total_area_km2": total_area_km2,
            "size_bytes": output_path.stat().st_size,
        },
        "completed_constraints": [
            {
                "constraint": "administrative_boundary",
                "status": "completed",
                "description": "Candidates are within Beijing administrative boundary.",
            },
            {
                "constraint": "exclude_water",
                "status": "completed",
                "description": "Water bodies from local OSM/Geofabrik data were excluded.",
            },
            {
                "constraint": "max_slope_deg",
                "status": "completed",
                "threshold": 8.0,
                "description": "Slope screening was calculated from Copernicus DEM GLO-30.",
            },
            {
                "constraint": "exclude_protected_areas",
                "status": "completed",
                "description": "OSM protected-area polygons were excluded.",
            },
        ],
        "skipped_constraints": [
            {
                "constraint": "max_distance_to_substation_km",
                "requested_threshold_km": 10.0,
                "status": "skipped_not_evaluated",
                "reason": (
                    "The local Beijing GeoPackage does not contain reliable "
                    "power=substation / substation geometry data. User explicitly "
                    "chose to skip this constraint instead of downloading or adding "
                    "a new data source."
                ),
                "impact": (
                    "The current candidates must not be described as satisfying "
                    "the 10 km substation-distance requirement."
                ),
            },
            {
                "constraint": "exclude_prime_farmland",
                "requested_setting": True,
                "status": "skipped_not_evaluated",
                "reason": (
                    "The current local data workflow does not include a reliable "
                    "prime farmland or land-use restriction layer. This constraint "
                    "was recorded as skipped rather than assumed to be satisfied."
                ),
                "impact": (
                    "The current candidates must not be described as satisfying "
                    "prime farmland or land-use exclusion requirements."
                ),
            },
        ],
        "input_source": {
            "path": relative_to_project(latest_candidate_path),
            "sha256": expected_sha256,
            "candidate_count": protected_metadata["output"]["feature_count"],
            "total_area_km2": protected_metadata["output"]["total_area_km2"],
        },
        "important_note": (
            "This is an available-data preliminary candidate set, not a final "
            "recommended site list. Grid-distance feasibility has not been evaluated."
        ),
    }

    write_json_atomic(decision_log_path, decision_log)

    return FinalizationResult(
        job_id=job_id,
        output_path=output_path,
        decision_log_path=decision_log_path,
        output_sha256=decision_log["latest_candidate_output"]["sha256"],
        candidate_count=candidate_count,
        total_area_km2=total_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = finalize_available_data_candidates(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "output_path": str(result.output_path),
                "decision_log_path": str(result.decision_log_path),
                "output_sha256": result.output_sha256,
                "candidate_count": result.candidate_count,
                "total_area_km2": round(result.total_area_km2, 6),
                "status": FINAL_STATUS,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

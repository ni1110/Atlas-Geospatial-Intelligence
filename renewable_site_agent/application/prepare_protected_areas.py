"""Prepare protected-area exclusion polygons for a site-selection job."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pyogrio
from shapely import make_valid

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.evidence import Evidence, EvidenceType
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


OSM_GPKG_PATH = RUNTIME_ROOT / "reference" / "osm" / "beijing.gpkg"
PROTECTED_LAYER = "gis_osm_protected_areas_a_free"


class ProtectedAreaPreparationError(RuntimeError):
    """Raised when protected-area preparation fails."""


@dataclass(frozen=True)
class ProtectedAreaResult:
    job_id: str
    output_path: Path
    metadata_path: Path
    output_sha256: str
    source_feature_count: int
    clipped_feature_count: int
    union_area_km2: float


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


def find_evidence(
    evidence_items: list[Evidence],
    evidence_type: EvidenceType,
) -> Evidence:
    evidence = next(
        (item for item in evidence_items if item.evidence_type == evidence_type),
        None,
    )

    if evidence is None:
        raise ProtectedAreaPreparationError(
            f"Missing required evidence: {evidence_type.value}"
        )

    return evidence


def resolve_verified_artifact(evidence: Evidence) -> Path:
    path = Path(evidence.artifact_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    path = path.resolve()

    if not path.is_file():
        raise ProtectedAreaPreparationError(f"Artifact does not exist: {path}")

    if sha256_file(path) != evidence.checksum_sha256:
        raise ProtectedAreaPreparationError(
            f"Evidence checksum mismatch: {evidence.evidence_type.value}"
        )

    return path


def prepare_protected_areas(job_id: str) -> ProtectedAreaResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise ProtectedAreaPreparationError(
            "Protected-area preparation requires candidates_ready; "
            f"received {job.status.value}."
        )

    if not OSM_GPKG_PATH.is_file():
        raise ProtectedAreaPreparationError(f"Missing OSM GeoPackage: {OSM_GPKG_PATH}")

    layers = [str(item[0]) for item in pyogrio.list_layers(OSM_GPKG_PATH)]
    if PROTECTED_LAYER not in layers:
        raise ProtectedAreaPreparationError(
            f"Missing protected-area layer: {PROTECTED_LAYER}"
        )

    boundary_evidence = find_evidence(
        job.evidence,
        EvidenceType.ADMINISTRATIVE_BOUNDARY,
    )
    boundary_path = resolve_verified_artifact(boundary_evidence)

    protected_dir = RUNTIME_ROOT / "jobs" / job_id / "protected_areas"
    output_path = protected_dir / "protected_areas_exclusion.geojson"
    metadata_path = protected_dir / "protected_areas_metadata.json"

    protected = pyogrio.read_dataframe(
        OSM_GPKG_PATH,
        layer=PROTECTED_LAYER,
    )
    boundary = gpd.read_file(boundary_path)

    if protected.crs is None:
        protected = protected.set_crs("EPSG:4326")
    if boundary.crs is None:
        boundary = boundary.set_crs("EPSG:4326")

    source_feature_count = len(protected)

    protected_metric = protected.to_crs("EPSG:32650").copy()
    boundary_metric = boundary.to_crs("EPSG:32650").copy()

    protected_metric = protected_metric[
        protected_metric.geometry.notna()
        & ~protected_metric.geometry.is_empty
    ].copy()

    protected_metric["geometry"] = protected_metric.geometry.apply(make_valid)
    boundary_union = make_valid(boundary_metric.geometry.union_all())

    protected_metric["geometry"] = protected_metric.geometry.intersection(boundary_union)

    protected_metric = protected_metric[
        protected_metric.geometry.notna()
        & ~protected_metric.geometry.is_empty
    ].copy()

    if protected_metric.empty:
        raise ProtectedAreaPreparationError(
            "No protected-area geometry remains after clipping to Beijing boundary."
        )

    protected_metric["geometry"] = protected_metric.geometry.apply(make_valid)
    protected_metric["area_km2"] = protected_metric.geometry.area / 1_000_000.0

    clipped_feature_count = len(protected_metric)
    raw_clipped_area_km2 = float(protected_metric["area_km2"].sum())
    protected_union = make_valid(protected_metric.geometry.union_all())
    union_area_km2 = float(protected_union.area / 1_000_000.0)

    dissolved = gpd.GeoDataFrame(
        {
            "layer_role": ["protected_area_exclusion"],
            "source_provider": ["Geofabrik / OpenStreetMap"],
            "source_layer": [PROTECTED_LAYER],
            "source_feature_count": [source_feature_count],
            "clipped_feature_count": [clipped_feature_count],
            "raw_clipped_area_km2": [raw_clipped_area_km2],
            "union_area_km2": [union_area_km2],
        },
        geometry=[protected_union],
        crs="EPSG:32650",
    )

    dissolved_wgs84 = dissolved.to_crs("EPSG:4326")
    write_geojson_atomic(dissolved_wgs84, output_path)

    fclass_counts = (
        protected["fclass"].value_counts(dropna=False).to_dict()
        if "fclass" in protected.columns
        else {}
    )

    metadata = {
        "job_id": job_id,
        "stage": "protected_area_preparation",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": {
            "provider": "Geofabrik / OpenStreetMap",
            "dataset_path": relative_to_project(OSM_GPKG_PATH),
            "dataset_sha256": sha256_file(OSM_GPKG_PATH),
            "layer": PROTECTED_LAYER,
            "source_license": "ODbL 1.0",
            "source_attribution": "© OpenStreetMap contributors",
            "source_feature_count": source_feature_count,
            "fclass_counts": fclass_counts,
        },
        "boundary_evidence": {
            "evidence_id": boundary_evidence.evidence_id,
            "artifact_path": boundary_evidence.artifact_path,
            "sha256": boundary_evidence.checksum_sha256,
        },
        "method": {
            "operation": "clip_and_dissolve",
            "metric_crs": "EPSG:32650",
            "description": (
                "Protected-area polygons from the local Beijing OSM GeoPackage are "
                "clipped to the Beijing administrative boundary and dissolved into "
                "one exclusion layer."
            ),
        },
        "output": {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "feature_count": 1,
            "clipped_source_feature_count": clipped_feature_count,
            "raw_clipped_area_km2": raw_clipped_area_km2,
            "union_area_km2": union_area_km2,
            "size_bytes": output_path.stat().st_size,
        },
        "warning": (
            "This layer uses OpenStreetMap protected-area data. It is suitable for "
            "preliminary screening but should be cross-checked with official protected-area "
            "registers before final site decisions."
        ),
    }

    write_json_atomic(metadata_path, metadata)

    return ProtectedAreaResult(
        job_id=job_id,
        output_path=output_path,
        metadata_path=metadata_path,
        output_sha256=metadata["output"]["sha256"],
        source_feature_count=source_feature_count,
        clipped_feature_count=clipped_feature_count,
        union_area_km2=union_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = prepare_protected_areas(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "output_path": str(result.output_path),
                "metadata_path": str(result.metadata_path),
                "output_sha256": result.output_sha256,
                "source_feature_count": result.source_feature_count,
                "clipped_feature_count": result.clipped_feature_count,
                "union_area_km2": round(result.union_area_km2, 6),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

"""Prepare a Beijing-specific DEM raster for slope screening."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import rasterio
from rasterio.enums import Resampling
from rasterio.mask import mask
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.evidence import Evidence, EvidenceType
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


NODATA_VALUE = -9999.0
DEFAULT_RESOLUTION_M = 30.0


class TerrainPreparationError(RuntimeError):
    """Raised when DEM preparation fails."""


@dataclass(frozen=True)
class TerrainDemResult:
    job_id: str
    output_path: Path
    metadata_path: Path
    output_sha256: str
    output_size_bytes: int
    crs: str
    width: int
    height: int
    resolution_m: float


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
        raise TerrainPreparationError(
            f"Missing required evidence: {evidence_type.value}"
        )

    return evidence


def resolve_verified_artifact(evidence: Evidence) -> Path:
    path = Path(evidence.artifact_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    path = path.resolve()

    if not path.is_file():
        raise TerrainPreparationError(
            f"Evidence artifact does not exist: {path}"
        )

    actual_sha256 = sha256_file(path)

    if actual_sha256 != evidence.checksum_sha256:
        raise TerrainPreparationError(
            "Evidence checksum mismatch: "
            f"{evidence.evidence_type.value}"
        )

    return path


def load_dem_manifest(dem_dir: Path) -> dict[str, Any]:
    manifest_path = dem_dir / "dem_manifest.json"

    if not manifest_path.is_file():
        raise TerrainPreparationError(
            f"Missing DEM manifest: {manifest_path}"
        )

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    records = manifest.get("records", [])

    if len(records) != 9:
        raise TerrainPreparationError(
            f"Expected 9 DEM records, got {len(records)}."
        )

    for record in records:
        path = dem_dir / record["filename"]

        if not path.is_file():
            raise TerrainPreparationError(
                f"Missing DEM tile: {path}"
            )

        if sha256_file(path) != record["sha256"]:
            raise TerrainPreparationError(
                f"DEM checksum mismatch: {path.name}"
            )

    return manifest


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def prepare_terrain_dem(
    job_id: str,
    *,
    resolution_m: float = DEFAULT_RESOLUTION_M,
) -> TerrainDemResult:
    if resolution_m <= 0:
        raise ValueError("resolution_m must be greater than zero.")

    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)

    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise TerrainPreparationError(
            "Terrain DEM preparation requires candidates_ready; "
            f"received {job.status.value}."
        )

    boundary_evidence = find_evidence(
        job.evidence,
        EvidenceType.ADMINISTRATIVE_BOUNDARY,
    )
    candidate_evidence = find_evidence(
        job.evidence,
        EvidenceType.CANDIDATE_SET,
    )

    boundary_path = resolve_verified_artifact(boundary_evidence)

    target_crs = candidate_evidence.metadata.get("metric_crs")

    if not target_crs:
        raise TerrainPreparationError(
            "Candidate evidence does not contain metric_crs."
        )

    dem_dir = (
        RUNTIME_ROOT
        / "reference"
        / "dem"
        / "copernicus_glo30"
        / "beijing"
    )

    manifest = load_dem_manifest(dem_dir)

    terrain_dir = RUNTIME_ROOT / "jobs" / job_id / "terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)

    wgs84_bbox_path = terrain_dir / "beijing_dem_wgs84_bbox.tmp.tif"
    utm_bbox_path = terrain_dir / "beijing_dem_utm50_bbox.tmp.tif"
    output_path = terrain_dir / "beijing_dem_utm50.tif"
    metadata_path = terrain_dir / "terrain_metadata.json"

    boundary = gpd.read_file(boundary_path)

    if boundary.crs is None:
        boundary = boundary.set_crs("EPSG:4326")

    # geoBoundaries metadata order:
    # south, north, west, east
    south, north, west, east = map(
        float,
        boundary_evidence.metadata["bounding_box"],
    )

    # Keep a small geographic buffer before reprojection to avoid edge loss.
    buffer_deg = 0.02
    merge_bounds = (
        west - buffer_deg,
        south - buffer_deg,
        east + buffer_deg,
        north + buffer_deg,
    )

    tile_paths = [
        dem_dir / record["filename"]
        for record in manifest["records"]
    ]

    sources = [rasterio.open(path) for path in tile_paths]

    try:
        crs_values = {str(source.crs) for source in sources}

        if len(crs_values) != 1:
            raise TerrainPreparationError(
                f"Inconsistent DEM CRS values: {crs_values}"
            )

        mosaic, mosaic_transform = merge(
            sources,
            bounds=merge_bounds,
            nodata=NODATA_VALUE,
            dtype="float32",
            resampling=Resampling.bilinear,
        )

        profile = sources[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            count=1,
            dtype="float32",
            crs=sources[0].crs,
            transform=mosaic_transform,
            nodata=NODATA_VALUE,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            bigtiff="IF_SAFER",
        )

        with rasterio.open(wgs84_bbox_path, "w", **profile) as dst:
            dst.write(mosaic[0], 1)

    finally:
        for source in sources:
            source.close()

    with rasterio.open(wgs84_bbox_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs,
            target_crs,
            src.width,
            src.height,
            *src.bounds,
            resolution=(resolution_m, resolution_m),
        )

        profile = src.profile.copy()
        profile.update(
            crs=target_crs,
            transform=transform,
            width=width,
            height=height,
            dtype="float32",
            nodata=NODATA_VALUE,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            bigtiff="IF_SAFER",
        )

        with rasterio.open(utm_bbox_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=NODATA_VALUE,
                dst_transform=transform,
                dst_crs=target_crs,
                dst_nodata=NODATA_VALUE,
                resampling=Resampling.bilinear,
            )

    boundary_metric = boundary.to_crs(target_crs)
    geometries = [
        geometry.__geo_interface__
        for geometry in boundary_metric.geometry
        if geometry is not None and not geometry.is_empty
    ]

    if not geometries:
        raise TerrainPreparationError(
            "Boundary geometry is empty after reprojection."
        )

    with rasterio.open(utm_bbox_path) as src:
        clipped, clipped_transform = mask(
            src,
            geometries,
            crop=True,
            nodata=NODATA_VALUE,
            filled=True,
        )

        profile = src.profile.copy()
        profile.update(
            height=clipped.shape[1],
            width=clipped.shape[2],
            transform=clipped_transform,
            dtype="float32",
            nodata=NODATA_VALUE,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            bigtiff="IF_SAFER",
        )

        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(clipped)

    wgs84_bbox_path.unlink(missing_ok=True)
    utm_bbox_path.unlink(missing_ok=True)

    with rasterio.open(output_path) as dataset:
        data = dataset.read(1, masked=True)

        if data.count() == 0:
            raise TerrainPreparationError(
                "Prepared DEM contains no valid pixels."
            )

        output_info = {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "size_bytes": output_path.stat().st_size,
            "crs": str(dataset.crs),
            "width": dataset.width,
            "height": dataset.height,
            "bounds": {
                "west": dataset.bounds.left,
                "south": dataset.bounds.bottom,
                "east": dataset.bounds.right,
                "north": dataset.bounds.top,
            },
            "resolution": {
                "x": abs(dataset.transform.a),
                "y": abs(dataset.transform.e),
                "unit": "metre",
            },
            "nodata": dataset.nodata,
            "valid_pixel_count": int(data.count()),
            "elevation_min_m": float(data.min()),
            "elevation_max_m": float(data.max()),
            "elevation_mean_m": float(data.mean()),
        }

    metadata = {
        "job_id": job_id,
        "stage": "terrain_dem_preparation",
        "created_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source_dem": {
            "dataset": manifest.get("dataset"),
            "data_type": manifest.get("data_type"),
            "tile_count": manifest.get("tile_count"),
            "manifest_path": relative_to_project(
                dem_dir / "dem_manifest.json"
            ),
            "manifest_sha256": sha256_file(
                dem_dir / "dem_manifest.json"
            ),
            "records": manifest.get("records"),
            "note": (
                "Copernicus DEM GLO-30 is a Digital Surface Model. "
                "It is suitable for preliminary slope screening but "
                "does not replace engineering survey data."
            ),
        },
        "boundary_evidence": {
            "evidence_id": boundary_evidence.evidence_id,
            "artifact_path": boundary_evidence.artifact_path,
            "sha256": boundary_evidence.checksum_sha256,
            "bounding_box": boundary_evidence.metadata.get(
                "bounding_box"
            ),
        },
        "processing": {
            "source_crs": "EPSG:4326",
            "target_crs": target_crs,
            "target_resolution_m": resolution_m,
            "merge_bounds_wgs84": {
                "west": merge_bounds[0],
                "south": merge_bounds[1],
                "east": merge_bounds[2],
                "north": merge_bounds[3],
            },
            "steps": [
                "merge_dem_tiles_with_boundary_buffer",
                "reproject_to_metric_crs",
                "clip_to_administrative_boundary",
                "write_compressed_float32_geotiff",
            ],
            "temporary_files_removed": True,
        },
        "output_dem": output_info,
    }

    write_json_atomic(metadata_path, metadata)

    return TerrainDemResult(
        job_id=job_id,
        output_path=output_path,
        metadata_path=metadata_path,
        output_sha256=output_info["sha256"],
        output_size_bytes=output_info["size_bytes"],
        crs=output_info["crs"],
        width=output_info["width"],
        height=output_info["height"],
        resolution_m=resolution_m,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument(
        "--resolution-m",
        type=float,
        default=DEFAULT_RESOLUTION_M,
    )

    args = parser.parse_args()

    result = prepare_terrain_dem(
        args.job_id,
        resolution_m=args.resolution_m,
    )

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "output_path": str(result.output_path),
                "metadata_path": str(result.metadata_path),
                "output_sha256": result.output_sha256,
                "output_size_mb": round(
                    result.output_size_bytes / 1024 / 1024,
                    2,
                ),
                "crs": result.crs,
                "width": result.width,
                "height": result.height,
                "resolution_m": result.resolution_m,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

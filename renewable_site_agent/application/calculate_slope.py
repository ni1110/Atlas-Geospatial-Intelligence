"""Calculate slope degrees and slope suitability mask from prepared DEM."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


SLOPE_NODATA = -9999.0
MASK_NODATA = 255


class SlopeCalculationError(RuntimeError):
    """Raised when slope calculation cannot be completed."""


@dataclass(frozen=True)
class SlopeCalculationResult:
    job_id: str
    slope_path: Path
    mask_path: Path
    metadata_path: Path
    slope_sha256: str
    mask_sha256: str
    max_slope_deg: float
    suitable_area_km2: float
    unsuitable_area_km2: float


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


def load_terrain_metadata(terrain_dir: Path) -> dict[str, Any]:
    metadata_path = terrain_dir / "terrain_metadata.json"

    if not metadata_path.is_file():
        raise SlopeCalculationError(
            f"Missing terrain metadata: {metadata_path}"
        )

    return json.loads(metadata_path.read_text(encoding="utf-8"))


def calculate_slope(
    job_id: str,
) -> SlopeCalculationResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise SlopeCalculationError(
            "Slope calculation requires candidates_ready; "
            f"received {job.status.value}."
        )

    request = (
        job.request.model_dump(mode="json")
        if hasattr(job.request, "model_dump")
        else dict(job.request)
    )

    max_slope_deg = float(request["max_slope_deg"])

    terrain_dir = RUNTIME_ROOT / "jobs" / job_id / "terrain"
    dem_path = terrain_dir / "beijing_dem_utm50.tif"
    slope_path = terrain_dir / "slope_degrees.tif"
    mask_path = terrain_dir / f"slope_suitable_mask_{max_slope_deg:g}deg.tif"
    metadata_path = terrain_dir / "slope_metadata.json"

    if not dem_path.is_file():
        raise SlopeCalculationError(
            f"Prepared DEM does not exist: {dem_path}"
        )

    terrain_metadata = load_terrain_metadata(terrain_dir)
    expected_dem_sha256 = terrain_metadata["output_dem"]["sha256"]

    if sha256_file(dem_path) != expected_dem_sha256:
        raise SlopeCalculationError(
            "Prepared DEM checksum does not match terrain metadata."
        )

    with rasterio.open(dem_path) as src:
        if src.crs is None:
            raise SlopeCalculationError("Prepared DEM has no CRS.")

        dx = abs(float(src.transform.a))
        dy = abs(float(src.transform.e))

        if dx <= 0 or dy <= 0:
            raise SlopeCalculationError(
                f"Invalid DEM resolution: dx={dx}, dy={dy}"
            )

        dem = src.read(1, masked=True).astype("float32")
        elevation = dem.filled(np.nan).astype("float32")

        # dz_dy: elevation change per metre in north-south direction
        # dz_dx: elevation change per metre in east-west direction
        dz_dy, dz_dx = np.gradient(elevation, dy, dx)

        slope_radians = np.arctan(
            np.sqrt((dz_dx * dz_dx) + (dz_dy * dz_dy))
        )
        slope_degrees = np.degrees(slope_radians).astype("float32")

        valid_slope = np.isfinite(slope_degrees)

        slope_output = np.where(
            valid_slope,
            slope_degrees,
            SLOPE_NODATA,
        ).astype("float32")

        suitable = valid_slope & (slope_degrees < max_slope_deg)
        unsuitable = valid_slope & (slope_degrees >= max_slope_deg)

        mask_output = np.full(
            slope_degrees.shape,
            MASK_NODATA,
            dtype="uint8",
        )
        mask_output[unsuitable] = 0
        mask_output[suitable] = 1

        slope_profile = src.profile.copy()
        slope_profile.update(
            dtype="float32",
            nodata=SLOPE_NODATA,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            bigtiff="IF_SAFER",
        )

        with rasterio.open(slope_path, "w", **slope_profile) as dst:
            dst.write(slope_output, 1)

        mask_profile = src.profile.copy()
        mask_profile.update(
            dtype="uint8",
            nodata=MASK_NODATA,
            compress="deflate",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            bigtiff="IF_SAFER",
        )

        with rasterio.open(mask_path, "w", **mask_profile) as dst:
            dst.write(mask_output, 1)

        pixel_area_km2 = (dx * dy) / 1_000_000.0

        valid_pixel_count = int(valid_slope.sum())
        suitable_pixel_count = int(suitable.sum())
        unsuitable_pixel_count = int(unsuitable.sum())

        suitable_area_km2 = suitable_pixel_count * pixel_area_km2
        unsuitable_area_km2 = unsuitable_pixel_count * pixel_area_km2

        valid_values = slope_degrees[valid_slope]

        if valid_values.size == 0:
            raise SlopeCalculationError(
                "Slope raster contains no valid slope pixels."
            )

        metadata = {
            "job_id": job_id,
            "stage": "slope_calculation",
            "created_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "input_dem": {
                "path": relative_to_project(dem_path),
                "sha256": expected_dem_sha256,
                "crs": str(src.crs),
                "width": src.width,
                "height": src.height,
                "resolution_m": {
                    "x": dx,
                    "y": dy,
                },
            },
            "method": {
                "description": (
                    "Slope is calculated from the prepared DEM by "
                    "estimating elevation gradients in x/y directions "
                    "and converting arctangent gradient magnitude to degrees."
                ),
                "threshold_source": "user_request",
                "max_slope_deg": max_slope_deg,
                "suitable_rule": "slope_degrees < max_slope_deg",
                "nodata_value_slope": SLOPE_NODATA,
                "nodata_value_mask": MASK_NODATA,
                "mask_values": {
                    "1": "suitable_slope",
                    "0": "unsuitable_slope",
                    "255": "nodata",
                },
            },
            "statistics": {
                "valid_pixel_count": valid_pixel_count,
                "suitable_pixel_count": suitable_pixel_count,
                "unsuitable_pixel_count": unsuitable_pixel_count,
                "pixel_area_km2": pixel_area_km2,
                "suitable_area_km2": suitable_area_km2,
                "unsuitable_area_km2": unsuitable_area_km2,
                "slope_min_deg": float(np.nanmin(valid_values)),
                "slope_max_deg": float(np.nanmax(valid_values)),
                "slope_mean_deg": float(np.nanmean(valid_values)),
                "slope_p50_deg": float(np.nanpercentile(valid_values, 50)),
                "slope_p90_deg": float(np.nanpercentile(valid_values, 90)),
                "slope_p95_deg": float(np.nanpercentile(valid_values, 95)),
            },
            "outputs": {
                "slope_degrees": {
                    "path": relative_to_project(slope_path),
                    "sha256": sha256_file(slope_path),
                    "size_bytes": slope_path.stat().st_size,
                },
                "slope_suitable_mask": {
                    "path": relative_to_project(mask_path),
                    "sha256": sha256_file(mask_path),
                    "size_bytes": mask_path.stat().st_size,
                },
            },
            "warning": (
                "Copernicus DEM GLO-30 is a Digital Surface Model. "
                "Slope screening is preliminary and does not replace "
                "engineering-grade terrain survey."
            ),
        }

    write_json_atomic(metadata_path, metadata)

    return SlopeCalculationResult(
        job_id=job_id,
        slope_path=slope_path,
        mask_path=mask_path,
        metadata_path=metadata_path,
        slope_sha256=metadata["outputs"]["slope_degrees"]["sha256"],
        mask_sha256=metadata["outputs"]["slope_suitable_mask"]["sha256"],
        max_slope_deg=max_slope_deg,
        suitable_area_km2=suitable_area_km2,
        unsuitable_area_km2=unsuitable_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = calculate_slope(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "slope_path": str(result.slope_path),
                "mask_path": str(result.mask_path),
                "metadata_path": str(result.metadata_path),
                "slope_sha256": result.slope_sha256,
                "mask_sha256": result.mask_sha256,
                "max_slope_deg": result.max_slope_deg,
                "suitable_area_km2": round(
                    result.suitable_area_km2,
                    6,
                ),
                "unsuitable_area_km2": round(
                    result.unsuitable_area_km2,
                    6,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

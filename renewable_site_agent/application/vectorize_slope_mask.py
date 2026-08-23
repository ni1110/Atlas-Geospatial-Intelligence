"""Vectorize slope suitability mask into a verified GeoJSON polygon layer."""

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
from rasterio.features import shapes
from shapely import make_valid
from shapely.geometry import shape

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


SUITABLE_VALUE = 1
MASK_NODATA = 255


class SlopeVectorizationError(RuntimeError):
    """Raised when slope mask vectorization fails."""


@dataclass(frozen=True)
class SlopeVectorizationResult:
    job_id: str
    output_path: Path
    metadata_path: Path
    output_sha256: str
    feature_count: int
    suitable_area_km2: float


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
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def write_geojson_atomic(frame: gpd.GeoDataFrame, output_path: Path) -> None:
    temporary_path = output_path.with_name(output_path.stem + ".tmp.geojson")
    temporary_path.unlink(missing_ok=True)

    frame.to_file(temporary_path, driver="GeoJSON")
    temporary_path.replace(output_path)


def vectorize_slope_mask(job_id: str) -> SlopeVectorizationResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise SlopeVectorizationError(
            "Slope vectorization requires candidates_ready; "
            f"received {job.status.value}."
        )

    terrain_dir = RUNTIME_ROOT / "jobs" / job_id / "terrain"
    slope_metadata_path = terrain_dir / "slope_metadata.json"

    if not slope_metadata_path.is_file():
        raise SlopeVectorizationError(
            f"Missing slope metadata: {slope_metadata_path}"
        )

    slope_metadata = json.loads(
        slope_metadata_path.read_text(encoding="utf-8")
    )

    mask_path = PROJECT_ROOT / slope_metadata["outputs"]["slope_suitable_mask"]["path"]
    expected_mask_sha256 = slope_metadata["outputs"]["slope_suitable_mask"]["sha256"]

    if not mask_path.is_file():
        raise SlopeVectorizationError(f"Missing slope mask: {mask_path}")

    if sha256_file(mask_path) != expected_mask_sha256:
        raise SlopeVectorizationError(
            "Slope mask checksum does not match slope metadata."
        )

    output_path = terrain_dir / "slope_suitable_area.geojson"
    metadata_path = terrain_dir / "slope_vector_metadata.json"

    geometries = []

    with rasterio.open(mask_path) as dataset:
        mask_array = dataset.read(1)
        suitable_mask = mask_array == SUITABLE_VALUE

        if not suitable_mask.any():
            raise SlopeVectorizationError(
                "Slope suitable mask contains no suitable pixels."
            )

        for geometry, value in shapes(
            mask_array,
            mask=suitable_mask,
            transform=dataset.transform,
        ):
            if int(value) != SUITABLE_VALUE:
                continue

            geom = make_valid(shape(geometry))

            if geom.is_empty:
                continue

            geometries.append(geom)

        if not geometries:
            raise SlopeVectorizationError(
                "Vectorization produced no valid geometries."
            )

        frame = gpd.GeoDataFrame(
            {
                "layer_role": ["slope_suitable_area"] * len(geometries),
                "max_slope_deg": [
                    slope_metadata["method"]["max_slope_deg"]
                ] * len(geometries),
                "source_mask_sha256": [expected_mask_sha256] * len(geometries),
            },
            geometry=geometries,
            crs=dataset.crs,
        )

    # Dissolve adjacent suitable polygons into one layer-level geometry.
    dissolved_geometry = make_valid(frame.geometry.union_all())

    dissolved = gpd.GeoDataFrame(
        {
            "layer_role": ["slope_suitable_area"],
            "max_slope_deg": [slope_metadata["method"]["max_slope_deg"]],
            "source_mask_sha256": [expected_mask_sha256],
        },
        geometry=[dissolved_geometry],
        crs=frame.crs,
    )

    if dissolved.geometry.iloc[0].is_empty:
        raise SlopeVectorizationError(
            "Dissolved slope suitable geometry is empty."
        )

    area_km2 = float(
        dissolved.to_crs(slope_metadata["input_dem"]["crs"])
        .geometry.area.sum()
        / 1_000_000.0
    )

    write_geojson_atomic(dissolved, output_path)

    metadata = {
        "job_id": job_id,
        "stage": "slope_mask_vectorization",
        "created_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "input_mask": {
            "path": relative_to_project(mask_path),
            "sha256": expected_mask_sha256,
            "value_used": SUITABLE_VALUE,
            "nodata_value": MASK_NODATA,
        },
        "method": {
            "description": (
                "Raster mask pixels with value 1 are converted to polygon "
                "features and dissolved into one slope-suitable layer."
            ),
            "suitable_rule": "slope_degrees < max_slope_deg",
            "max_slope_deg": slope_metadata["method"]["max_slope_deg"],
        },
        "source_raster_statistics": slope_metadata["statistics"],
        "output": {
            "path": relative_to_project(output_path),
            "sha256": sha256_file(output_path),
            "feature_count": int(len(dissolved)),
            "crs": str(dissolved.crs),
            "suitable_area_km2": area_km2,
            "size_bytes": output_path.stat().st_size,
        },
        "warning": (
            "This vector layer represents preliminary slope suitability "
            "derived from Copernicus DEM GLO-30 DSM."
        ),
    }

    write_json_atomic(metadata_path, metadata)

    return SlopeVectorizationResult(
        job_id=job_id,
        output_path=output_path,
        metadata_path=metadata_path,
        output_sha256=metadata["output"]["sha256"],
        feature_count=metadata["output"]["feature_count"],
        suitable_area_km2=area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = vectorize_slope_mask(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "output_path": str(result.output_path),
                "metadata_path": str(result.metadata_path),
                "output_sha256": result.output_sha256,
                "feature_count": result.feature_count,
                "suitable_area_km2": round(result.suitable_area_km2, 6),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

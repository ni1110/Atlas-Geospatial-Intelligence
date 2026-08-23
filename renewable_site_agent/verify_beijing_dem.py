from __future__ import annotations

import hashlib
import json
from pathlib import Path

import rasterio

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.domain.evidence import EvidenceType
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"
DEM_DIR = (
    RUNTIME_ROOT
    / "reference"
    / "dem"
    / "copernicus_glo30"
    / "beijing"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def main() -> None:
    manifest_path = DEM_DIR / "dem_manifest.json"

    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    records = manifest.get("records", [])
    if len(records) != 9:
        raise RuntimeError(f"Expected 9 DEM records, got {len(records)}")

    tif_paths = sorted(DEM_DIR.glob("*.tif"))
    if len(tif_paths) != 9:
        raise RuntimeError(f"Expected 9 tif files, got {len(tif_paths)}")

    print("========== DEM MANIFEST ==========")
    print("dataset =", manifest.get("dataset"))
    print("data_type =", manifest.get("data_type"))
    print("tile_count =", manifest.get("tile_count"))

    print("\n========== TILE CHECK ==========")

    all_west = []
    all_south = []
    all_east = []
    all_north = []
    crs_values = set()
    resolution_values = set()
    total_size = 0

    for record in records:
        path = DEM_DIR / record["filename"]

        if not path.is_file():
            raise FileNotFoundError(f"Missing tile: {path}")

        actual_sha256 = sha256_file(path)
        if actual_sha256 != record["sha256"]:
            raise RuntimeError(
                "SHA-256 mismatch: "
                f"{path.name}"
            )

        with rasterio.open(path) as dataset:
            bounds = dataset.bounds
            crs = str(dataset.crs)
            resolution = (
                round(abs(dataset.transform.a), 12),
                round(abs(dataset.transform.e), 12),
            )

            crs_values.add(crs)
            resolution_values.add(resolution)

            all_west.append(bounds.left)
            all_south.append(bounds.bottom)
            all_east.append(bounds.right)
            all_north.append(bounds.top)

            total_size += path.stat().st_size

            print(
                path.name,
                "| crs =", crs,
                "| width =", dataset.width,
                "| height =", dataset.height,
                "| res =", resolution,
                "| nodata =", dataset.nodata,
                "| bounds =",
                (
                    round(bounds.left, 6),
                    round(bounds.bottom, 6),
                    round(bounds.right, 6),
                    round(bounds.top, 6),
                ),
            )

    if len(crs_values) != 1:
        raise RuntimeError(f"DEM CRS is inconsistent: {crs_values}")

    if len(resolution_values) != 1:
        raise RuntimeError(
            f"DEM resolution is inconsistent: {resolution_values}"
        )

    union_bounds = {
        "west": min(all_west),
        "south": min(all_south),
        "east": max(all_east),
        "north": max(all_north),
    }

    print("\n========== DEM UNION COVERAGE ==========")
    print("union_bounds =", union_bounds)
    print("crs =", next(iter(crs_values)))
    print("resolution =", next(iter(resolution_values)))
    print("total_size_mb =", round(total_size / 1024 / 1024, 2))

    store = SQLiteJobStore(RUNTIME_ROOT / "site_jobs.db")
    job = store.get(JOB_ID)

    boundary_evidence = next(
        item
        for item in job.evidence
        if item.evidence_type == EvidenceType.ADMINISTRATIVE_BOUNDARY
    )

    # geoBoundaries metadata order:
    # south, north, west, east
    south, north, west, east = map(
        float,
        boundary_evidence.metadata["bounding_box"],
    )

    print("\n========== BEIJING BOUNDARY ==========")
    print(
        "boundary_bbox =",
        {
            "west": west,
            "south": south,
            "east": east,
            "north": north,
        },
    )

    covers_beijing = (
        union_bounds["west"] <= west
        and union_bounds["south"] <= south
        and union_bounds["east"] >= east
        and union_bounds["north"] >= north
    )

    print("\n========== VERDICT ==========")
    print("covers_beijing =", covers_beijing)

    if not covers_beijing:
        raise RuntimeError("DEM tiles do not fully cover Beijing boundary.")

    print("PASS: DEM tiles are complete, readable, checksum-verified, and cover Beijing.")


if __name__ == "__main__":
    main()

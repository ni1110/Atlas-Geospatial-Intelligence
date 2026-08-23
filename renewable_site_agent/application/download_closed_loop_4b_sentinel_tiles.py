"""Download real Sentinel-2 image tiles for Closed Loop 4B.

This uses Microsoft Planetary Computer STAC API and Sentinel-2 L2A visual assets.
It downloads real imagery for the 4A sampled candidate bboxes and saves them as
PNG files under runtime/remote_sensing/tile_inputs/<sample_id>.png.

No fake imagery is generated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import planetary_computer
import rasterio
from PIL import Image
from pystac_client import Client
from rasterio.windows import from_bounds
from rasterio.warp import transform_bounds

from renewable_site_agent.application.create_job import RUNTIME_ROOT


STAC_ENDPOINT = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stretch_to_uint8(arr: np.ndarray) -> np.ndarray:
    arr = arr.astype("float32")
    out = np.zeros_like(arr, dtype="uint8")

    for c in range(arr.shape[0]):
        band = arr[c]
        valid = np.isfinite(band)
        if not np.any(valid):
            continue

        lo, hi = np.percentile(band[valid], [2, 98])
        if hi <= lo:
            hi = lo + 1.0

        scaled = (band - lo) / (hi - lo)
        scaled = np.clip(scaled, 0, 1)
        out[c] = (scaled * 255).astype("uint8")

    return out


def _read_rgb_chip(
    href: str,
    bbox_wgs84: dict[str, float],
    output_size: int,
) -> np.ndarray:
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
    ):
        with rasterio.open(href) as src:
            dst_bounds = transform_bounds(
                "EPSG:4326",
                src.crs,
                bbox_wgs84["min_lon"],
                bbox_wgs84["min_lat"],
                bbox_wgs84["max_lon"],
                bbox_wgs84["max_lat"],
                densify_pts=21,
            )

            window = from_bounds(*dst_bounds, transform=src.transform)

            if src.count >= 3:
                indexes = [1, 2, 3]
            else:
                indexes = [1]

            arr = src.read(
                indexes=indexes,
                window=window,
                out_shape=(len(indexes), output_size, output_size),
                boundless=True,
                fill_value=0,
            )

            if arr.shape[0] == 1:
                arr = np.repeat(arr, 3, axis=0)

            if arr.dtype != np.uint8:
                arr = _stretch_to_uint8(arr)
            else:
                arr = arr.astype("uint8")

            # CHW -> HWC
            rgb = np.transpose(arr[:3], (1, 2, 0))
            return rgb


def _search_best_item(
    catalog: Client,
    bbox: list[float],
    date_start: str,
    date_end: str,
    max_cloud: float,
):
    search = catalog.search(
        collections=[COLLECTION],
        bbox=bbox,
        datetime=f"{date_start}/{date_end}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        max_items=10,
    )

    items = list(search.items())
    if not items:
        return None

    items.sort(key=lambda item: item.properties.get("eo:cloud_cover", 9999))
    return items[0]


def download_sentinel_tiles(
    date_start: str,
    date_end: str,
    max_cloud: float,
    output_size: int,
    limit: int | None,
) -> dict[str, Any]:
    tile_manifest_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_tile_request_manifest_v1.json"
    )
    tile_manifest = read_json(tile_manifest_path)

    tile_input_dir = RUNTIME_ROOT / "remote_sensing" / "tile_inputs"
    tile_input_dir.mkdir(parents=True, exist_ok=True)

    catalog = Client.open(STAC_ENDPOINT)

    records = []
    tasks = tile_manifest.get("tile_tasks", [])
    if limit is not None:
        tasks = tasks[:limit]

    for task in tasks:
        sample_id = task["sample_id"]
        bbox_dict = task["bbox_wgs84"]
        bbox = [
            bbox_dict["min_lon"],
            bbox_dict["min_lat"],
            bbox_dict["max_lon"],
            bbox_dict["max_lat"],
        ]

        output_path = tile_input_dir / f"{sample_id}.png"

        record = {
            "sample_id": sample_id,
            "bbox_wgs84": bbox_dict,
            "output_path": str(output_path),
            "download_status": "pending",
            "item_id": None,
            "datetime": None,
            "cloud_cover": None,
            "asset_key": None,
            "failure_reason": None,
        }

        try:
            item = _search_best_item(
                catalog=catalog,
                bbox=bbox,
                date_start=date_start,
                date_end=date_end,
                max_cloud=max_cloud,
            )

            if item is None:
                record["download_status"] = "missing_no_stac_item"
                record["failure_reason"] = "No Sentinel-2 item found for bbox/time/cloud filter."
                records.append(record)
                continue

            signed_item = planetary_computer.sign(item)

            if "visual" in signed_item.assets:
                asset_key = "visual"
            elif "TCI" in signed_item.assets:
                asset_key = "TCI"
            else:
                record["download_status"] = "missing_visual_asset"
                record["failure_reason"] = f"No visual/TCI asset. Available assets: {list(signed_item.assets.keys())}"
                records.append(record)
                continue

            href = signed_item.assets[asset_key].href

            rgb = _read_rgb_chip(
                href=href,
                bbox_wgs84=bbox_dict,
                output_size=output_size,
            )

            Image.fromarray(rgb).save(output_path)

            record["download_status"] = "downloaded"
            record["item_id"] = item.id
            record["datetime"] = item.properties.get("datetime")
            record["cloud_cover"] = item.properties.get("eo:cloud_cover")
            record["asset_key"] = asset_key

        except Exception as exc:
            record["download_status"] = "failed"
            record["failure_reason"] = f"{type(exc).__name__}: {exc}"

        records.append(record)

    downloaded_count = sum(1 for r in records if r["download_status"] == "downloaded")
    failed_count = sum(1 for r in records if r["download_status"] == "failed")
    missing_count = len(records) - downloaded_count - failed_count

    if downloaded_count == len(records) and len(records) > 0:
        status = "completed"
    elif downloaded_count > 0:
        status = "partially_completed"
    else:
        status = "failed_or_no_imagery"

    report = {
        "sentinel_download_manifest_id": "closed_loop_4b_sentinel2_download_manifest_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "download_status": status,
        "stac_endpoint": STAC_ENDPOINT,
        "collection": COLLECTION,
        "date_start": date_start,
        "date_end": date_end,
        "max_cloud": max_cloud,
        "output_size": output_size,
        "tile_input_dir": str(tile_input_dir),
        "requested_tile_count": len(records),
        "downloaded_tile_count": downloaded_count,
        "missing_tile_count": missing_count,
        "failed_tile_count": failed_count,
        "do_not_fake_imagery": True,
        "records": records,
    }

    report_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_sentinel2_download_manifest_v1.json"
    )
    write_json(report_path, report)

    return {
        "sentinel_download_status": status,
        "requested_tile_count": len(records),
        "downloaded_tile_count": downloaded_count,
        "missing_tile_count": missing_count,
        "failed_tile_count": failed_count,
        "tile_input_dir": str(tile_input_dir),
        "download_manifest_path": str(report_path),
        "next_step_zh": "重新运行 4B tile acquisition，把已下载真实 Sentinel-2 图片登记进系统。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-start", default="2025-01-01")
    parser.add_argument("--date-end", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--max-cloud", type=float, default=40.0)
    parser.add_argument("--output-size", type=int, default=512)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    result = download_sentinel_tiles(
        date_start=args.date_start,
        date_end=args.date_end,
        max_cloud=args.max_cloud,
        output_size=args.output_size,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

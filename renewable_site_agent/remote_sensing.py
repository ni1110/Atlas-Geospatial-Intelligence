"""Current-job Sentinel-2 verification; historical reports are never consulted."""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import shape

from renewable_site_agent.config import PATHS
from renewable_site_agent.data.manager import DataSourceManager
from renewable_site_agent.schemas import DataArtifact, ErrorCode, ToolResult


def _asset_href(assets: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        asset = assets.get(name)
        if isinstance(asset, dict) and asset.get("href"):
            return str(asset["href"])
    return None


def _read_window(href: str, bounds: tuple[float, float, float, float], size: int = 512) -> tuple[np.ndarray, dict[str, Any]]:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    with rasterio.open(href) as source:
        source_bounds = bounds
        if str(source.crs) != "EPSG:4326":
            source_bounds = transform_bounds("EPSG:4326", source.crs, *bounds, densify_pts=21)
        window = from_bounds(*source_bounds, transform=source.transform)
        height = max(1, min(size, int(round(window.height))))
        width = max(1, min(size, int(round(window.width))))
        data = source.read(
            1, window=window, out_shape=(height, width),
            resampling=Resampling.bilinear, boundless=True, fill_value=source.nodata or 0,
        ).astype("float32")
        return data, {
            "source_crs": str(source.crs), "source_resolution": list(source.res),
            "window_shape": [height, width],
        }


def _scale_rgb(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    channels = []
    for band in (red, green, blue):
        valid = band[np.isfinite(band) & (band > 0)]
        if not valid.size:
            channels.append(np.zeros_like(band, dtype="uint8"))
            continue
        low, high = np.percentile(valid, [2, 98])
        if high <= low:
            high = low + 1
        channels.append(np.clip((band - low) / (high - low) * 255, 0, 255).astype("uint8"))
    return np.stack(channels, axis=-1)


def _vlm_verify(chip: Path, candidate_id: str) -> dict[str, Any] | None:
    base_url = os.getenv("VLM_BASE_URL") or os.getenv("GEOAGENT_VLM_BASE_URL")
    model = os.getenv("VLM_MODEL_ID", "Qwen2.5-VL-7B-Instruct")
    if not base_url:
        return None
    image_data = base64.b64encode(chip.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Assess only visible land-cover evidence for renewable site screening. Return JSON with suitability, risks, and confidence. Do not infer legal status."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
                ],
            }
        ],
    }
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.getenv('VLM_API_KEY', 'EMPTY')}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read())
    content = body["choices"][0]["message"]["content"]
    result = json.loads(content)
    result["candidate_id"] = candidate_id
    return result


def verify_current_job(
    *,
    job_id: str,
    candidates_geojson: dict[str, Any],
    require_vlm: bool = False,
    date_range: str = "2025-01-01/2026-12-31",
    cloud_cover: float = 20,
) -> ToolResult:
    features = candidates_geojson.get("features", [])
    if not features:
        return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "No candidates for remote-sensing verification")
    bounds = [shape(feature["geometry"]).bounds for feature in features]
    bbox = [
        min(value[0] for value in bounds), min(value[1] for value in bounds),
        max(value[2] for value in bounds), max(value[3] for value in bounds),
    ]
    search = DataSourceManager().acquire(
        "sentinel",
        {"bbox": bbox, "date_range": date_range, "cloud_cover": cloud_cover, "limit": 10},
    )
    if search.status == "failed":
        return ToolResult(
            status="degraded", degraded=True,
            message="Current-job Sentinel provider unavailable; no historical result substituted",
            error_code=search.error_code,
            metadata={
                "job_id": job_id, "verification": "unavailable",
                "needs_human_review": bool(require_vlm),
                "provider_error": search.model_dump(mode="json"),
                "current_job_search": True,
            },
        )
    artifact = search.artifact if isinstance(search.artifact, DataArtifact) else None
    if artifact is None:
        return ToolResult.fail(ErrorCode.INVALID_OUTPUT, "Sentinel search lacks DataArtifact")
    items = artifact.metadata.get("items", [])
    items.sort(key=lambda item: float(item.get("properties", {}).get("eo:cloud_cover", 100)))
    item = items[0]
    assets = item.get("assets", {})
    red_href = _asset_href(assets, ("B04", "red"))
    green_href = _asset_href(assets, ("B03", "green"))
    blue_href = _asset_href(assets, ("B02", "blue"))
    nir_href = _asset_href(assets, ("B08", "nir"))
    if not all((red_href, green_href, blue_href, nir_href)):
        return ToolResult(
            status="degraded", degraded=True,
            message="Sentinel item lacks required RGB/NIR assets",
            artifact=artifact, provenance=[artifact],
            metadata={"job_id": job_id, "verification": "unavailable", "needs_human_review": bool(require_vlm), "item_id": item.get("id")},
        )
    output_dir = PATHS.runtime / "jobs" / job_id / "remote_sensing"
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence = []
    degraded = False
    for feature in features:
        candidate_id = str(feature.get("properties", {}).get("candidate_id"))
        candidate_bounds = shape(feature["geometry"]).bounds
        try:
            red, raster_metadata = _read_window(red_href, candidate_bounds)
            green, _ = _read_window(green_href, candidate_bounds)
            blue, _ = _read_window(blue_href, candidate_bounds)
            nir, _ = _read_window(nir_href, candidate_bounds)
            common_height = min(red.shape[0], green.shape[0], blue.shape[0], nir.shape[0])
            common_width = min(red.shape[1], green.shape[1], blue.shape[1], nir.shape[1])
            red, green, blue, nir = [band[:common_height, :common_width] for band in (red, green, blue, nir)]
            denominator = nir + red
            ndvi = np.where(np.abs(denominator) > 1e-6, (nir - red) / denominator, np.nan)
            valid = np.isfinite(ndvi)
            chip_path = PATHS.assert_artifact_path(output_dir / f"{candidate_id}.png")
            from PIL import Image

            Image.fromarray(_scale_rgb(red, green, blue), mode="RGB").save(chip_path, optimize=True)
            vlm = None
            try:
                vlm = _vlm_verify(chip_path, candidate_id)
            except Exception as exc:
                degraded = True
                vlm = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
            evidence.append(
                {
                    "candidate_id": candidate_id,
                    "item_id": item.get("id"),
                    "chip_path": str(chip_path),
                    "deterministic_evidence": {
                        "mean_ndvi": float(np.nanmean(ndvi)) if valid.any() else None,
                        "vegetation_fraction_ndvi_gt_0_3": float(np.mean(ndvi[valid] > 0.3)) if valid.any() else None,
                        "valid_fraction": float(valid.mean()),
                        **raster_metadata,
                    },
                    "vlm_evidence": vlm or {"status": "optional_capability_unavailable"},
                }
            )
            if require_vlm and vlm is None:
                degraded = True
        except Exception as exc:
            degraded = True
            evidence.append({"candidate_id": candidate_id, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"})
    result_path = PATHS.assert_artifact_path(output_dir / "verification.json")
    payload = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "current_job_search": True,
        "sentinel_item_id": item.get("id"),
        "evidence": evidence,
        "degraded": degraded,
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ToolResult(
        status="degraded" if degraded else "success",
        degraded=degraded,
        message="Current-job Sentinel verification completed" if not degraded else "Current-job verification completed with unavailable evidence",
        artifact={"path": str(result_path), "result": payload},
        provenance=[artifact],
        metadata={
            "job_id": job_id, "current_job_search": True,
            "needs_human_review": bool(require_vlm and degraded),
            "candidate_count": len(features), "verified_count": sum("deterministic_evidence" in value for value in evidence),
        },
    )

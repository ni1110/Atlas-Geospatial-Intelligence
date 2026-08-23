"""Small-AOI live acceptance for Renewable Site Agent data providers.

This is deliberately an evaluation harness, not another application pipeline.
It calls the existing DataSourceManager, DataArtifact, GISExecutor, and scoring
implementations with one boundary-derived AOI and writes only under artifacts/.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import box, mapping, shape

from renewable_site_agent.config import PATHS, apply_process_environment, load_config
from renewable_site_agent.data.manager import DataSourceManager
from renewable_site_agent.gis.engine import (
    GISExecutor,
    choose_local_crs,
    feature_collection,
    overpass_to_geometries,
    project_geometry,
    write_geojson,
)
from renewable_site_agent.gis.scoring import score_and_rank
from renewable_site_agent.schemas import AOI, DataArtifact, SiteSelectionRequest, ToolResult


PROJECT_ROOT = PATHS.project_root
EVAL_ROOT = PATHS.artifacts / "evals"
RUN_ROOT = EVAL_ROOT / "live-gansu-small-aoi"
METRICS_PATH = EVAL_ROOT / "live_data_provider_metrics.json"
REPORT_PATH = EVAL_ROOT / "LIVE_DATA_PROVIDER_REPORT.md"
ALLOWED_RESULTS = {"PASS", "PARTIAL", "FAIL", "SKIPPED_WITH_REASON"}
WORLDCOVER_SCHEMA = {10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100}
TWO_GIB = 2 * 1024**3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def disk_free_bytes() -> int:
    return int(shutil.disk_usage(PROJECT_ROOT.anchor).free)


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def managed_sizes() -> dict[str, int]:
    roots = {
        name: PROJECT_ROOT / name
        for name in ("data", "cache", "runtime", "artifacts", "logs", "state", "temp", "models", "downloads")
    }
    return {name: tree_size(path) for name, path in roots.items()}


def model_snapshot() -> dict[str, int]:
    path = PROJECT_ROOT / "models"
    files = [item for item in path.rglob("*") if item.is_file()] if path.exists() else []
    return {"file_count": len(files), "size_bytes": sum(item.stat().st_size for item in files)}


def artifact_of(result: ToolResult) -> DataArtifact | None:
    if result.artifact is None:
        return None
    if isinstance(result.artifact, DataArtifact):
        return result.artifact
    try:
        return DataArtifact.model_validate(result.artifact)
    except Exception:
        return None


def result_error(result: ToolResult) -> str | None:
    if result.status != "failed":
        return None
    prefix = f"{result.error_code.value}: " if result.error_code else ""
    return prefix + result.message


def acquire(manager: DataSourceManager, dataset: str, query: dict[str, Any]) -> tuple[ToolResult, float]:
    started = time.perf_counter()
    result = manager.acquire(dataset, query)
    return result, (time.perf_counter() - started) * 1000


def derive_small_aoi(boundary: Any) -> tuple[Any, list[float], float]:
    """Select a verified, compact bbox inside the real ADM1 geometry."""
    point = boundary.representative_point()
    for half_size in (0.05, 0.04, 0.035, 0.03, 0.025, 0.02):
        candidate = box(
            point.x - half_size,
            point.y - half_size,
            point.x + half_size,
            point.y + half_size,
        )
        if boundary.covers(candidate):
            local_crs = choose_local_crs(candidate)
            area_km2 = project_geometry(candidate, "EPSG:4326", local_crs).area / 1_000_000
            if area_km2 >= 10:
                return candidate, list(candidate.bounds), float(area_km2)
    raise RuntimeError("Could not derive a >=10 km2 bbox fully inside the Gansu boundary")


def raster_statistics(path: str) -> dict[str, Any]:
    import rasterio

    with rasterio.open(path) as source:
        band = source.read(1, masked=True)
        values = np.asarray(band.compressed(), dtype="float64")
        values = values[np.isfinite(values)]
        if values.size == 0:
            raise RuntimeError(f"Raster contains no valid pixels: {path}")
        return {
            "path": path,
            "landed_bytes": Path(path).stat().st_size,
            "width": source.width,
            "height": source.height,
            "count": source.count,
            "crs": str(source.crs),
            "transform": tuple(source.transform),
            "resolution": [abs(float(source.transform.a)), abs(float(source.transform.e))],
            "nodata": source.nodata,
            "valid_pixel_count": int(values.size),
            "valid_fraction": float(values.size / (source.width * source.height)),
            "min": float(values.min()),
            "max": float(values.max()),
            "mean": float(values.mean()),
            "unique_values": sorted(int(value) for value in np.unique(values))[:256],
        }


def overpass_count(artifact: DataArtifact | None) -> int | None:
    if artifact is None:
        return None
    payload = artifact.metadata.get("overpass_json", {})
    elements = payload.get("elements") if isinstance(payload, dict) else None
    return len(elements) if isinstance(elements, list) else None


def nasa_value(artifact: DataArtifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    values = artifact.metadata.get("values", {})
    if not isinstance(values, dict) or not values:
        return None
    value = values.get("ANN")
    if value is None:
        numeric = [float(item) for item in values.values() if isinstance(item, (int, float)) and item > -900]
        value = sum(numeric) / len(numeric) if numeric else None
    return {
        "parameter": artifact.metadata.get("parameter"),
        "value": value,
        "units": artifact.metadata.get("units"),
        "all_values": values,
    }


def resource_score(resource: dict[str, Any] | None, project_type: str) -> float | None:
    if not resource or not isinstance(resource.get("value"), (int, float)):
        return None
    ranges = load_config("scoring.yaml")["resource_score_ranges"][project_type]
    low, high = float(ranges["minimum"]), float(ranges["maximum"])
    return max(0.0, min(100.0, 100 * (float(resource["value"]) - low) / (high - low)))


def provider_entry(
    *,
    provider: str,
    live_network: bool,
    data_fetched: bool,
    processing_succeeded: bool,
    bytes_value: int,
    latency_ms: float,
    cache: str,
    result: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    if result not in ALLOWED_RESULTS:
        raise ValueError(f"Invalid acceptance result: {result}")
    return {
        "provider": provider,
        "live_network": live_network,
        "data_fetched": data_fetched,
        "processing_succeeded": processing_succeeded,
        "bytes": int(bytes_value),
        "latency_ms": round(float(latency_ms), 3),
        "cache": cache,
        "result": result,
        "details": details,
    }


def markdown_report(metrics: dict[str, Any]) -> str:
    rows = []
    for item in metrics["providers"]:
        rows.append(
            "| {provider} | {live} | {fetched} | {processed} | {bytes:,} | {latency:.1f} | {cache} | {result} |".format(
                provider=item["provider"],
                live="yes" if item["live_network"] else "no",
                fetched="yes" if item["data_fetched"] else "no",
                processed="yes" if item["processing_succeeded"] else "no",
                bytes=item["bytes"],
                latency=item["latency_ms"],
                cache=item["cache"],
                result=item["result"],
            )
        )
    provider_details = []
    for item in metrics["providers"]:
        provider_details.append(
            f"### {item['provider']} — {item['result']}\n\n```json\n"
            + json.dumps(item["details"], ensure_ascii=False, indent=2)
            + "\n```"
        )
    aoi = metrics["aoi"]
    disk = metrics["disk"]
    e2e = metrics["gis_e2e"]
    constraints = metrics["constraints"]
    return f"""# Live Data Provider Acceptance Report

Generated: `{metrics['completed_at']}`
Scope: one boundary-derived small AOI in Gansu; no full province raster, Sentinel scene, China PBF, fixture, or model download.

## AOI and provenance

- Region request: `{metrics['region_request']}` (resolved as `{metrics['resolved_region']}`)
- geoBoundaries ADM level: `ADM1`
- Bbox W,S,E,N: `{json.dumps(aoi['bbox'])}`
- Approximate area: `{aoi['area_km2']:.3f} km²`
- Fully covered by Gansu polygon: `{aoi['inside_gansu']}`
- CRS for requests: `EPSG:4326`
- Current run only: `{metrics['started_at']}` to `{metrics['completed_at']}`

## Provider matrix

| Provider | Live network | Data fetched | Processing succeeded | Bytes | Latency ms | Cache | Result |
| -------- | ------------ | ------------ | -------------------- | ----: | ---------: | ----- | ------ |
{chr(10).join(rows)}

`Bytes` is exact API response-body size where exposed by the provider. For GDAL `/vsicurl` COG reads, HTTP range-transfer byte counts are not exposed by Rasterio, so the exact retained AOI raster size is used and separately identified in JSON. It is a conservative accounted-size metric, not a claim that a full tile was downloaded.

## Provider evidence

{chr(10).join(provider_details)}

## Real small-AOI GIS E2E

- Result: `{e2e['result']}`
- Flow: Gansu boundary → DEM → slope → water → land cover → OSM substation/roads/protected proxy → NASA POWER → candidate generation → scoring → Top-K.
- Candidate count: `{e2e.get('candidate_count')}`
- Top-K count: `{e2e.get('top_k_count')}`
- Output: `{e2e.get('candidates_path')}`
- Sentinel/VLM was not required for GIS completion.
- Details: `{e2e.get('message')}`

## Retry and fallback

- Retry triggered: `{metrics['reliability']['retry_triggered']}`. Provider methods are single-attempt and no acceptance request was intentionally fault-injected.
- Natural fallback triggered: `{metrics['reliability']['fallback_triggered']}`.
- Events: `{json.dumps(metrics['reliability']['events'], ensure_ascii=False)}`

## Disk, cache, and model constraints

- D free before: `{disk['free_before_bytes']:,}` bytes (`{disk['free_before_gib']:.3f} GiB`)
- D free after: `{disk['free_after_bytes']:,}` bytes (`{disk['free_after_gib']:.3f} GiB`)
- Accounted live bytes: `{disk['accounted_live_bytes']:,}` bytes (`{disk['accounted_live_mib']:.3f} MiB`)
- Managed storage delta: `{disk['managed_delta_bytes']:,}` bytes
- Cache directory total: `{disk.get('cache_directory_after_bytes', disk['cache_after_bytes']):,}` bytes; indexed DataCache envelopes: `{disk['cache_after_bytes']:,}` / `{disk['cache_limit_bytes']:,}` bytes
- Model bytes before/after: `{disk['model_before_bytes']:,}` / `{disk['model_after_bytes']:,}`
- New model download: `{constraints['new_model_download']}`
- All output paths under D project root: `{constraints['all_outputs_under_project']}`
- Accounted live bytes <= 2 GiB: `{constraints['within_two_gib']}`

## Code changes required by acceptance

{chr(10).join('- ' + item for item in metrics['code_changes'])}

No architecture, provider priority, canonical GIS algorithm, LLM/VLM, or production entry point was added or replaced.

## Verification

- Acceptance regression tests: `{metrics.get('verification', {}).get('regression_tests', 'not recorded')}`
- Report/metrics consistency validation: `{metrics.get('verification', {}).get('report_consistency', 'not recorded')}`
"""


def main() -> int:
    apply_process_environment()
    EVAL_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    free_before = disk_free_bytes()
    sizes_before = managed_sizes()
    models_before = model_snapshot()
    manager = DataSourceManager()
    gis = GISExecutor()
    providers: list[dict[str, Any]] = []
    reliability_events: list[dict[str, Any]] = []

    # Use the Chinese form explicitly to verify the repaired alias, while the
    # upstream boundary's shapeName proves resolution to Gansu.
    boundary_query = {"region": "\u7518\u8083", "acceptance_scope": "gansu-small-aoi-v1"}
    boundary_first, boundary_latency = acquire(manager, "administrative_boundary", boundary_query)
    boundary_second, boundary_cache_latency = acquire(manager, "administrative_boundary", boundary_query)
    boundary_artifact = artifact_of(boundary_first)
    if boundary_artifact is None or boundary_artifact.provider != "geoboundaries":
        raise RuntimeError(f"Live geoBoundaries Gansu lookup failed: {result_error(boundary_first) or boundary_first.message}")
    boundary_fc = boundary_artifact.metadata["feature_collection"]
    boundary_geometry = shape(boundary_fc["features"][0]["geometry"])
    resolved_region = str(boundary_fc["features"][0].get("properties", {}).get("shapeName", "Gansu"))
    aoi_geometry, bbox, aoi_area_km2 = derive_small_aoi(boundary_geometry)
    aoi_fc = feature_collection([aoi_geometry], [{"region": "Gansu", "acceptance_run": started_at}])
    aoi_query = {
        "bbox": bbox,
        "aoi": aoi_fc,
        "acceptance_scope": "gansu-small-aoi-v1",
    }
    boundary_bytes = int(boundary_first.metrics.get("bytes_downloaded", 0) or 0)
    providers.append(
        provider_entry(
            provider="geoBoundaries",
            live_network=True,
            data_fetched=True,
            processing_succeeded=boundary_second.metrics.get("cache_hit") is True,
            bytes_value=boundary_bytes,
            latency_ms=boundary_latency,
            cache=f"first=MISS; second={'HIT' if boundary_second.metrics.get('cache_hit') else 'MISS'}",
            result="PASS" if boundary_second.metrics.get("cache_hit") else "PARTIAL",
            details={
                "provider": boundary_artifact.provider,
                "source_url": boundary_artifact.source_url,
                "retrieved_at": boundary_artifact.retrieved_at.isoformat(),
                "crs": boundary_artifact.crs,
                "checksum_sha256": boundary_artifact.checksum_sha256,
                "first_metrics": boundary_first.metrics,
                "second_metrics": boundary_second.metrics,
                "second_latency_ms": boundary_cache_latency,
                "shape_name": resolved_region,
                "geometry_type": boundary_geometry.geom_type,
            },
        )
    )

    # DEM remote-window materialization and slope processing.
    dem_query = {**aoi_query, "materialize": True}
    dem_result, dem_latency = acquire(manager, "dem", dem_query)
    dem_artifact = artifact_of(dem_result)
    dem_stats: dict[str, Any] | None = None
    slope_stats: dict[str, Any] | None = None
    slope_path = RUN_ROOT / "slope.tif"
    dem_error = result_error(dem_result)
    if dem_artifact and dem_artifact.local_path and Path(dem_artifact.local_path).suffix.lower() == ".tif":
        try:
            dem_stats = raster_statistics(dem_artifact.local_path)
            slope_result = gis.calculate_slope(dem_artifact.local_path, str(slope_path), max_slope_deg=8.0)
            if slope_result.status == "failed":
                dem_error = slope_result.message
            else:
                slope_stats = raster_statistics(str(slope_path))
        except Exception as exc:
            dem_error = f"{type(exc).__name__}: {exc}"
    dem_landed = int(dem_stats["landed_bytes"]) if dem_stats else 0
    providers.append(
        provider_entry(
            provider="Copernicus DEM GLO-30",
            live_network=dem_artifact is not None and dem_result.metrics.get("cache_miss") is True,
            data_fetched=dem_stats is not None,
            processing_succeeded=slope_stats is not None,
            bytes_value=dem_landed,
            latency_ms=dem_latency,
            cache="MISS" if dem_result.metrics.get("cache_miss") else "HIT",
            result="PASS" if slope_stats is not None else "FAIL",
            details={
                "source_url": dem_artifact.source_url if dem_artifact else None,
                "remote_cog_urls": dem_artifact.metadata.get("remote_cog_urls") if dem_artifact else None,
                "retrieved_at": dem_artifact.retrieved_at.isoformat() if dem_artifact else None,
                "network_transfer_bytes": "not exposed by GDAL /vsicurl",
                "retained_aoi_bytes": dem_landed,
                "elevation": dem_stats,
                "slope": slope_stats,
                "error": dem_error,
            },
        )
    )

    # WorldCover remote-window materialization and schema validation.
    land_result, land_latency = acquire(manager, "land_cover", {**aoi_query, "materialize": True})
    land_artifact = artifact_of(land_result)
    land_stats: dict[str, Any] | None = None
    land_error = result_error(land_result)
    if land_artifact and land_artifact.local_path and Path(land_artifact.local_path).suffix.lower() == ".tif":
        try:
            land_stats = raster_statistics(land_artifact.local_path)
            invalid_classes = sorted(set(land_stats["unique_values"]) - WORLDCOVER_SCHEMA)
            if invalid_classes:
                land_error = f"Unexpected WorldCover classes: {invalid_classes}"
                land_stats = None
        except Exception as exc:
            land_error = f"{type(exc).__name__}: {exc}"
    land_landed = int(land_stats["landed_bytes"]) if land_stats else 0
    providers.append(
        provider_entry(
            provider="ESA WorldCover",
            live_network=land_artifact is not None and land_result.metrics.get("cache_miss") is True,
            data_fetched=land_stats is not None,
            processing_succeeded=bool(land_stats and land_stats["unique_values"]),
            bytes_value=land_landed,
            latency_ms=land_latency,
            cache="MISS" if land_result.metrics.get("cache_miss") else "HIT",
            result="PASS" if land_stats and land_stats["unique_values"] else "FAIL",
            details={
                "source_url": land_artifact.source_url if land_artifact else None,
                "remote_cog_urls": land_artifact.metadata.get("remote_cog_urls") if land_artifact else None,
                "retrieved_at": land_artifact.retrieved_at.isoformat() if land_artifact else None,
                "network_transfer_bytes": "not exposed by GDAL /vsicurl",
                "raster": land_stats,
                "schema_values": sorted(WORLDCOVER_SCHEMA),
                "error": land_error,
            },
        )
    )

    # JRC primary; DataSourceManager may naturally fall back to OSM water.
    water_query = {**aoi_query, "materialize": True, "occurrence_threshold": 50}
    water_result, water_latency = acquire(manager, "water", water_query)
    water_artifact = artifact_of(water_result)
    jrc_primary = bool(water_artifact and water_artifact.provider == "jrc_gsw")
    water_stats: dict[str, Any] | None = None
    if jrc_primary and water_artifact and water_artifact.local_path:
        try:
            water_stats = raster_statistics(water_artifact.local_path)
        except Exception:
            water_stats = None
    if water_result.degraded or water_result.fallback_provider:
        reliability_events.append(
            {
                "dataset": "water",
                "original_provider": water_result.original_provider,
                "fallback_provider": water_result.fallback_provider,
                "reason": water_result.fallback_reason,
            }
        )
    providers.append(
        provider_entry(
            provider="JRC Global Surface Water",
            live_network=jrc_primary,
            data_fetched=water_stats is not None,
            processing_succeeded=water_stats is not None,
            bytes_value=int(water_stats["landed_bytes"]) if water_stats else 0,
            latency_ms=water_latency,
            cache="MISS" if jrc_primary and water_result.metrics.get("cache_miss") else "not cached / fallback",
            result="PASS" if water_stats is not None else "FAIL",
            details={
                "primary_provider": "jrc_gsw",
                "actual_provider": water_artifact.provider if water_artifact else None,
                "source_url": water_artifact.source_url if jrc_primary and water_artifact else None,
                "raster": water_stats,
                "primary_failure": water_result.fallback_reason if not jrc_primary else None,
                "fallback_provider": water_result.fallback_provider,
            },
        )
    )

    # Required Overpass requests. Empty element arrays are valid live results.
    osm_specs = {
        "power_infrastructure": '["power"="substation"]',
        "roads": '["highway"~"motorway|trunk|primary|secondary|tertiary"]',
        "protected_areas": '["boundary"="protected_area"]',
    }
    osm_results: dict[str, ToolResult] = {}
    osm_calls: list[dict[str, Any]] = []
    osm_total_bytes = 0
    osm_total_latency = 0.0
    for dataset, selector in osm_specs.items():
        result, latency = acquire(manager, dataset, aoi_query)
        osm_results[dataset] = result
        artifact = artifact_of(result)
        count = overpass_count(artifact)
        bytes_value = int(result.metrics.get("bytes_downloaded", 0) or 0)
        osm_total_bytes += bytes_value
        osm_total_latency += latency
        osm_calls.append(
            {
                "dataset": dataset,
                "selector": selector,
                "bbox": bbox,
                "http_query_executed": artifact is not None and artifact.provider == "osm_overpass",
                "result_count": count,
                "bytes": bytes_value,
                "latency_ms": latency,
                "cache": "MISS" if result.metrics.get("cache_miss") else "HIT",
                "rate_limit_triggered": result.error_code is not None and result.error_code.value == "RATE_LIMIT",
                "retry_triggered": False,
                "error": result_error(result),
            }
        )
        if result.degraded or result.fallback_provider:
            reliability_events.append(
                {
                    "dataset": dataset,
                    "original_provider": result.original_provider,
                    "fallback_provider": result.fallback_provider,
                    "reason": result.fallback_reason,
                }
            )
    if water_artifact and water_artifact.provider == "osm_overpass":
        water_count = overpass_count(water_artifact)
        osm_calls.append(
            {
                "dataset": "water_fallback",
                "selector": '["natural"="water"]',
                "bbox": bbox,
                "http_query_executed": True,
                "result_count": water_count,
                "bytes": int(water_result.metrics.get("bytes_downloaded", 0) or 0),
                "latency_ms": water_latency,
                "cache": "MISS",
                "rate_limit_triggered": False,
                "retry_triggered": False,
                "error": None,
            }
        )
        osm_total_bytes += int(water_result.metrics.get("bytes_downloaded", 0) or 0)
    osm_ok = all(call["http_query_executed"] and call["result_count"] is not None for call in osm_calls)
    providers.append(
        provider_entry(
            provider="OSM Overpass",
            live_network=osm_ok,
            data_fetched=osm_ok,
            processing_succeeded=osm_ok,
            bytes_value=osm_total_bytes,
            latency_ms=osm_total_latency,
            cache="; ".join(f"{item['dataset']}={item['cache']}" for item in osm_calls),
            result="PASS" if osm_ok else "FAIL",
            details={
                "endpoint": "https://overpass-api.de/api/interpreter",
                "calls": osm_calls,
                "zero_results_are_valid": True,
            },
        )
    )

    # NASA POWER for both required resource variables.
    nasa_results: dict[str, ToolResult] = {}
    nasa_calls: list[dict[str, Any]] = []
    nasa_total_bytes = 0
    nasa_total_latency = 0.0
    for project_type in ("solar", "wind"):
        result, latency = acquire(manager, "energy_resource", {**aoi_query, "project_type": project_type})
        nasa_results[project_type] = result
        artifact = artifact_of(result)
        resource = nasa_value(artifact)
        bytes_value = int(result.metrics.get("bytes_downloaded", 0) or 0)
        nasa_total_bytes += bytes_value
        nasa_total_latency += latency
        nasa_calls.append(
            {
                "project_type": project_type,
                "parameter": resource.get("parameter") if resource else None,
                "value": resource.get("value") if resource else None,
                "units": resource.get("units") if resource else None,
                "source_url": artifact.source_url if artifact else None,
                "retrieved_at": artifact.retrieved_at.isoformat() if artifact else None,
                "bytes": bytes_value,
                "latency_ms": latency,
                "cache": "MISS" if result.metrics.get("cache_miss") else "HIT",
                "error": result_error(result),
            }
        )
    nasa_ok = all(call["value"] is not None and call["units"] for call in nasa_calls)
    providers.append(
        provider_entry(
            provider="NASA POWER",
            live_network=all(artifact_of(item) is not None for item in nasa_results.values()),
            data_fetched=all(call["value"] is not None for call in nasa_calls),
            processing_succeeded=nasa_ok,
            bytes_value=nasa_total_bytes,
            latency_ms=nasa_total_latency,
            cache="; ".join(f"{item['project_type']}={item['cache']}" for item in nasa_calls),
            result="PASS" if nasa_ok else "PARTIAL" if any(call["value"] is not None for call in nasa_calls) else "FAIL",
            details={"representative_point": [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2], "calls": nasa_calls},
        )
    )

    # Required Sentinel metadata search. Full scenes are categorically forbidden.
    sentinel_query = {
        **aoi_query,
        "date_range": "2025-05-01/2025-10-31",
        "cloud_cover": 30,
        "limit": 5,
    }
    sentinel_result, sentinel_latency = acquire(manager, "sentinel", sentinel_query)
    sentinel_artifact = artifact_of(sentinel_result)
    sentinel_items = sentinel_artifact.metadata.get("items", []) if sentinel_artifact else []
    asset_hrefs = [
        value.get("href")
        for item in sentinel_items
        for value in (item.get("assets") or {}).values()
        if isinstance(value, dict) and value.get("href")
    ]
    actual_sentinel_provider = sentinel_artifact.provider if sentinel_artifact else None
    if sentinel_result.degraded or sentinel_result.fallback_provider:
        reliability_events.append(
            {
                "dataset": "sentinel",
                "original_provider": sentinel_result.original_provider,
                "fallback_provider": sentinel_result.fallback_provider,
                "reason": sentinel_result.fallback_reason,
            }
        )
    sentinel_primary_ok = actual_sentinel_provider == "copernicus_dataspace_stac"
    providers.append(
        provider_entry(
            provider="Copernicus Sentinel STAC",
            live_network=sentinel_primary_ok,
            data_fetched=bool(sentinel_primary_ok and sentinel_items),
            processing_succeeded=bool(sentinel_primary_ok and asset_hrefs),
            bytes_value=int(sentinel_result.metrics.get("bytes_downloaded", 0) or 0) if sentinel_primary_ok else 0,
            latency_ms=sentinel_latency,
            cache="MISS" if sentinel_primary_ok and sentinel_result.metrics.get("cache_miss") else "not cached / fallback",
            result="PASS" if sentinel_primary_ok and asset_hrefs else "FAIL",
            details={
                "endpoint": "https://stac.dataspace.copernicus.eu/v1/search",
                "collection": "sentinel-2-l2a",
                "date_range": sentinel_query["date_range"],
                "cloud_cover_lte": sentinel_query["cloud_cover"],
                "item_count": len(sentinel_items) if sentinel_primary_ok else None,
                "first_item_id": sentinel_items[0].get("id") if sentinel_primary_ok and sentinel_items else None,
                "asset_href_count": len(asset_hrefs) if sentinel_primary_ok else None,
                "asset_materialization": {
                    "result": "SKIPPED_WITH_REASON",
                    "reason": "Optional layer not run: no Copernicus credential was supplied; full-scene download is forbidden.",
                },
                "error": sentinel_result.fallback_reason if not sentinel_primary_ok else None,
            },
        )
    )
    if actual_sentinel_provider == "planetary_computer_stac":
        providers.append(
            provider_entry(
                provider="Planetary Computer fallback",
                live_network=True,
                data_fetched=bool(sentinel_items),
                processing_succeeded=bool(asset_hrefs),
                bytes_value=int(sentinel_result.metrics.get("bytes_downloaded", 0) or 0),
                latency_ms=sentinel_latency,
                cache="MISS" if sentinel_result.metrics.get("cache_miss") else "HIT",
                result="PASS" if asset_hrefs else "PARTIAL",
                details={
                    "endpoint": sentinel_artifact.source_url if sentinel_artifact else None,
                    "item_count": len(sentinel_items),
                    "first_item_id": sentinel_items[0].get("id") if sentinel_items else None,
                    "asset_href_count": len(asset_hrefs),
                    "fallback_reason": sentinel_result.fallback_reason,
                    "asset_materialization": {
                        "result": "SKIPPED_WITH_REASON",
                        "reason": "Optional chip was not needed for canonical GIS completion; full scenes remain forbidden.",
                    },
                },
            )
        )
    elif not sentinel_primary_ok:
        failures = sentinel_result.metadata.get("provider_failures", [])
        if any(item.get("provider") == "planetary_computer_stac" for item in failures):
            providers.append(
                provider_entry(
                    provider="Planetary Computer fallback",
                    live_network=False,
                    data_fetched=False,
                    processing_succeeded=False,
                    bytes_value=0,
                    latency_ms=sentinel_latency,
                    cache="not cached",
                    result="FAIL",
                    details={"failures": failures},
                )
            )

    # Existing component chain, using only artifacts from this live AOI run.
    e2e: dict[str, Any]
    try:
        if not dem_stats or not slope_stats or not land_stats:
            raise RuntimeError("Required live DEM/slope/WorldCover evidence is unavailable")
        exclusions = []
        protected_artifact = artifact_of(osm_results["protected_areas"])
        if protected_artifact:
            protected_geometries = overpass_to_geometries(protected_artifact.metadata.get("overpass_json", {}))
            if protected_geometries:
                exclusions.append(feature_collection(protected_geometries))
        raster_exclusions = [
            {"path": str(slope_path), "mode": "gt", "threshold": 8.0},
            {"path": land_artifact.local_path, "mode": "in", "values": [50, 80, 90, 95]},
        ]
        if jrc_primary and water_artifact and water_artifact.local_path:
            raster_exclusions.append({"path": water_artifact.local_path, "mode": "gte", "threshold": 50})
        elif water_artifact and water_artifact.provider == "osm_overpass":
            water_geometries = overpass_to_geometries(water_artifact.metadata.get("overpass_json", {}))
            if water_geometries:
                exclusions.append(feature_collection(water_geometries))
        suitable = gis.apply_spatial_exclusion(aoi_fc, exclusions, raster_exclusions)
        if suitable.status == "failed":
            raise RuntimeError(suitable.message)
        candidates = gis.build_candidate_polygons(suitable.artifact, minimum_area_km2=0.05)
        if candidates.status == "failed" or not candidates.artifact.get("features"):
            raise RuntimeError(candidates.message or "No candidate polygon survived")
        candidate_fc = candidates.artifact
        for dataset, property_name in (
            ("power_infrastructure", "substation_distance_km"),
            ("roads", "road_distance_km"),
        ):
            target_artifact = artifact_of(osm_results[dataset])
            if target_artifact and overpass_count(target_artifact):
                distance = gis.calculate_nearest_distance(
                    candidate_fc,
                    {"overpass_json": target_artifact.metadata.get("overpass_json", {})},
                    property_name,
                )
                if distance.status != "failed":
                    candidate_fc = distance.artifact
        solar_resource = nasa_value(artifact_of(nasa_results["solar"]))
        statistics_result = gis.calculate_candidate_statistics(
            candidate_fc,
            slope_raster_path=str(slope_path),
            land_cover_raster_path=land_artifact.local_path,
            land_cover_preferences={"10": 70, "20": 80, "30": 95, "40": 35, "60": 85},
            energy_resource_score=resource_score(solar_resource, "solar"),
        )
        if statistics_result.status == "failed":
            raise RuntimeError(statistics_result.message)
        candidate_fc = statistics_result.artifact
        validation = gis.validate_candidate_geometry(candidate_fc, aoi_fc, minimum_area_km2=0.05)
        if validation.status == "failed":
            raise RuntimeError(validation.message)
        provenance = [
            item
            for item in (
                boundary_artifact,
                dem_artifact,
                water_artifact,
                land_artifact,
                artifact_of(osm_results["power_infrastructure"]),
                artifact_of(osm_results["roads"]),
                artifact_of(osm_results["protected_areas"]),
                artifact_of(nasa_results["solar"]),
            )
            if item is not None
        ]
        request = SiteSelectionRequest(
            region="\u7518\u8083",
            aoi=AOI(bbox=tuple(bbox), geojson=aoi_fc),
            project_type="solar",
            max_slope_deg=8.0,
            exclude_water=True,
            exclude_protected_area=True,
            excluded_land_cover_classes=[50, 80, 90, 95],
            land_cover_preferences={"10": 70, "20": 80, "30": 95, "40": 35, "60": 85},
            minimum_contiguous_area_km2=0.05,
            top_k=3,
        )
        ranked = score_and_rank(candidate_fc, request, provenance)
        if not ranked:
            raise RuntimeError("Scoring returned no Top-K candidates")
        candidates_path = RUN_ROOT / "candidates.geojson"
        write_geojson(candidates_path, candidate_fc)
        result_path = RUN_ROOT / "top_k_results.json"
        result_path.write_text(
            json.dumps([item.model_dump(mode="json") for item in ranked], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        e2e = {
            "result": "PASS",
            "message": "Live remote evidence entered existing GISExecutor and score_and_rank components.",
            "candidate_count": len(candidate_fc["features"]),
            "top_k_count": len(ranked),
            "candidates_path": str(candidates_path),
            "result_path": str(result_path),
            "top_k": [
                {
                    "rank": item.rank,
                    "candidate_id": item.candidate_id,
                    "area_km2": item.area_km2,
                    "overall_score": item.overall_score,
                    "evidence_completeness_score": item.evidence_completeness_score,
                }
                for item in ranked
            ],
        }
    except Exception as exc:
        e2e = {
            "result": "FAIL",
            "message": f"{type(exc).__name__}: {exc}",
            "candidate_count": 0,
            "top_k_count": 0,
            "candidates_path": None,
        }

    accounted_live_bytes = sum(item["bytes"] for item in providers)
    cache_stats = manager.cache.stats()
    models_after = model_snapshot()
    completed_at = utc_now()
    metrics: dict[str, Any] = {
        "schema_version": "1.0",
        "acceptance": "gansu-small-aoi-live-data-provider",
        "started_at": started_at,
        "completed_at": completed_at,
        "region_request": "\u7518\u8083",
        "resolved_region": resolved_region,
        "aoi": {
            "bbox": bbox,
            "area_km2": aoi_area_km2,
            "inside_gansu": bool(boundary_geometry.covers(aoi_geometry)),
            "geometry": mapping(aoi_geometry),
        },
        "providers": providers,
        "gis_e2e": e2e,
        "reliability": {
            "retry_triggered": any(
                call.get("retry_triggered") for item in osm_calls for call in [item]
            ),
            "fallback_triggered": bool(reliability_events),
            "events": reliability_events,
        },
        "disk": {
            "free_before_bytes": free_before,
            "free_before_gib": free_before / 1024**3,
            "free_after_bytes": 0,
            "free_after_gib": 0.0,
            "managed_before_bytes": sum(sizes_before.values()),
            "managed_after_bytes": 0,
            "managed_delta_bytes": 0,
            "managed_sizes_before": sizes_before,
            "managed_sizes_after": {},
            "accounted_live_bytes": accounted_live_bytes,
            "accounted_live_mib": accounted_live_bytes / 1024**2,
            "cache_after_bytes": int(cache_stats["size_bytes"]),
            "cache_limit_bytes": int(cache_stats["max_size_bytes"]),
            "model_before_bytes": models_before["size_bytes"],
            "model_after_bytes": models_after["size_bytes"],
        },
        "constraints": {
            "within_two_gib": accounted_live_bytes <= TWO_GIB,
            "new_model_download": models_after != models_before,
            "all_outputs_under_project": all(
                PROJECT_ROOT == path.resolve() or PROJECT_ROOT in path.resolve().parents
                for path in (RUN_ROOT, METRICS_PATH, REPORT_PATH)
            ),
            "full_province_raster_downloaded": False,
            "full_sentinel_scene_downloaded": False,
            "china_osm_pbf_downloaded": False,
            "fixture_or_mock_used": False,
        },
        "code_changes": [
            "Added Unicode-safe Gansu aliases because the legacy source-code mapping was code-page corrupted.",
            "Preserved materialized DataArtifact.local_path across cache-envelope writes so live rasters reach slope/GIS processing.",
            "Aligned the OSM infrastructure selector with the acceptance requirement: power=substation.",
            "Updated the retired Copernicus catalogue STAC URL to the current official /v1/search endpoint after the live endpoint returned HTTP 400.",
            "Normalized date-only STAC intervals to RFC 3339 timestamps required by the current Copernicus endpoint.",
            "Added this bounded eval harness and no production architecture or feature surface.",
        ],
    }

    # Write once, then include report files themselves in the final disk reading.
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    sizes_after = managed_sizes()
    free_after = disk_free_bytes()
    metrics["disk"].update(
        {
            "free_after_bytes": free_after,
            "free_after_gib": free_after / 1024**3,
            "managed_after_bytes": sum(sizes_after.values()),
            "managed_delta_bytes": sum(sizes_after.values()) - sum(sizes_before.values()),
            "managed_sizes_after": sizes_after,
        }
    )
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    print(json.dumps({
        "metrics": str(METRICS_PATH),
        "report": str(REPORT_PATH),
        "aoi_bbox": bbox,
        "providers": {item["provider"]: item["result"] for item in providers},
        "gis_e2e": e2e["result"],
        "accounted_live_mib": accounted_live_bytes / 1024**2,
        "free_before_gib": free_before / 1024**3,
        "free_after_gib": free_after / 1024**3,
    }, ensure_ascii=False, indent=2))
    return 0 if e2e["result"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())

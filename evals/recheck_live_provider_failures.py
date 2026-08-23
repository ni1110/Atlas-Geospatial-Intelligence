"""Recheck only the failed live calls and merge evidence into the first-run report."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.run_live_data_provider_acceptance import (
    METRICS_PATH,
    REPORT_PATH,
    TWO_GIB,
    artifact_of,
    managed_sizes,
    markdown_report,
    model_snapshot,
    nasa_value,
    overpass_count,
)
from renewable_site_agent.config import PATHS, apply_process_environment
from renewable_site_agent.data.manager import DataSourceManager


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def by_name(providers: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in providers if item["provider"] == name)


def main() -> int:
    apply_process_environment()
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    manager = DataSourceManager()
    bbox = metrics["aoi"]["bbox"]
    aoi_fc = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"region": "Gansu"}, "geometry": metrics["aoi"]["geometry"]}],
    }
    base_query = {"bbox": bbox, "aoi": aoi_fc, "acceptance_scope": "gansu-small-aoi-v1"}

    osm = by_name(metrics["providers"], "OSM Overpass")
    roads_call = next(item for item in osm["details"]["calls"] if item["dataset"] == "roads")
    if osm["result"] != "PASS":
        # Only the failed major-roads query is sent again.
        started = time.perf_counter()
        roads = manager.acquire("roads", base_query)
        roads_latency = (time.perf_counter() - started) * 1000
        roads_artifact = artifact_of(roads)
        roads_call.update(
            {
                "http_query_executed": roads_artifact is not None and roads_artifact.provider == "osm_overpass",
                "result_count": overpass_count(roads_artifact),
                "bytes": int(roads.metrics.get("bytes_downloaded", 0) or 0),
                "latency_ms": roads_latency,
                "cache": "MISS" if roads.metrics.get("cache_miss") else "HIT",
                "rate_limit_triggered": bool(roads.error_code and roads.error_code.value == "RATE_LIMIT"),
                "retry_triggered": False,
                "targeted_recheck": True,
                "initial_failure": "PROVIDER_UNAVAILABLE: No provider succeeded for roads",
                "error": None if roads.status != "failed" else f"{roads.error_code}: {roads.message}",
                "provider_failures": roads.metadata.get("provider_failures", []),
            }
        )
    osm_ok = all(item["http_query_executed"] and item["result_count"] is not None for item in osm["details"]["calls"])
    osm["bytes"] = sum(int(item["bytes"]) for item in osm["details"]["calls"])
    osm["latency_ms"] = round(sum(float(item["latency_ms"]) for item in osm["details"]["calls"]), 3)
    osm["live_network"] = osm_ok
    osm["data_fetched"] = osm_ok
    osm["processing_succeeded"] = osm_ok
    osm["cache"] = "; ".join(f"{item['dataset']}={item['cache']}" for item in osm["details"]["calls"])
    osm["result"] = "PASS" if osm_ok else "FAIL"

    # Recheck only Copernicus after replacing its retired endpoint.
    sentinel_query = {
        **base_query,
        "date_range": "2025-05-01/2025-10-31",
        "cloud_cover": 30,
        "limit": 5,
    }
    started = time.perf_counter()
    sentinel = manager.acquire("sentinel", sentinel_query)
    sentinel_latency = (time.perf_counter() - started) * 1000
    sentinel_artifact = artifact_of(sentinel)
    items = sentinel_artifact.metadata.get("items", []) if sentinel_artifact else []
    hrefs = [
        value.get("href")
        for item in items
        for value in (item.get("assets") or {}).values()
        if isinstance(value, dict) and value.get("href")
    ]
    copernicus = by_name(metrics["providers"], "Copernicus Sentinel STAC")
    copernicus_ok = bool(sentinel_artifact and sentinel_artifact.provider == "copernicus_dataspace_stac" and hrefs)
    copernicus.update(
        {
            "live_network": copernicus_ok,
            "data_fetched": bool(copernicus_ok and items),
            "processing_succeeded": copernicus_ok,
            "bytes": int(sentinel.metrics.get("bytes_downloaded", 0) or 0) if copernicus_ok else 0,
            "latency_ms": round(sentinel_latency, 3),
            "cache": "MISS" if sentinel.metrics.get("cache_miss") else "HIT",
            "result": "PASS" if copernicus_ok else "FAIL",
        }
    )
    copernicus["details"].update(
        {
            "endpoint": "https://stac.dataspace.copernicus.eu/v1/search",
            "item_count": len(items) if copernicus_ok else None,
            "first_item_id": items[0].get("id") if copernicus_ok and items else None,
            "asset_href_count": len(hrefs) if copernicus_ok else None,
            "error": None if copernicus_ok else (sentinel.fallback_reason or sentinel.message),
            "targeted_recheck": True,
            "initial_endpoint": "https://catalogue.dataspace.copernicus.eu/stac/search",
            "initial_error": "HTTPError: HTTP Error 400: Bad Request",
            "endpoint_fix_reference": "https://documentation.dataspace.copernicus.eu/APIs/STAC.html",
        }
    )

    if not any(item.get("event") == "targeted_recheck_after_initial_failure" for item in metrics["reliability"]["events"]):
        metrics["reliability"]["events"].append(
            {
                "dataset": "roads",
                "event": "targeted_recheck_after_initial_failure",
                "result": osm["result"],
                "automatic_retry": False,
            }
        )
    prior_sentinel_recheck = next(
        (item for item in metrics["reliability"]["events"] if item.get("event") == "provider_endpoint_corrected_then_targeted_recheck"),
        None,
    )
    if prior_sentinel_recheck:
        prior_sentinel_recheck["result"] = copernicus["result"]
    else:
        metrics["reliability"]["events"].append(
            {
                "dataset": "sentinel",
                "event": "provider_endpoint_corrected_then_targeted_recheck",
                "result": copernicus["result"],
                "automatic_retry": False,
            }
        )
    endpoint_change = "Updated the retired Copernicus catalogue STAC URL to the current official /v1/search endpoint after the live endpoint returned HTTP 400."
    if endpoint_change not in metrics["code_changes"]:
        metrics["code_changes"].insert(-1, endpoint_change)
    datetime_change = "Normalized date-only STAC intervals to RFC 3339 timestamps required by the current Copernicus endpoint."
    if datetime_change not in metrics["code_changes"]:
        metrics["code_changes"].insert(-1, datetime_change)
    metrics["completed_at"] = now()
    metrics["disk"]["accounted_live_bytes"] = sum(int(item["bytes"]) for item in metrics["providers"])
    metrics["disk"]["accounted_live_mib"] = metrics["disk"]["accounted_live_bytes"] / 1024**2
    cache_stats = manager.cache.stats()
    metrics["disk"]["cache_after_bytes"] = int(cache_stats["size_bytes"])
    metrics["constraints"]["within_two_gib"] = metrics["disk"]["accounted_live_bytes"] <= TWO_GIB
    metrics["constraints"]["new_model_download"] = model_snapshot()["size_bytes"] != metrics["disk"]["model_before_bytes"]

    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    sizes_after = managed_sizes()
    free_after = int(shutil.disk_usage(PATHS.project_root.anchor).free)
    metrics["disk"].update(
        {
            "free_after_bytes": free_after,
            "free_after_gib": free_after / 1024**3,
            "managed_after_bytes": sum(sizes_after.values()),
            "managed_delta_bytes": sum(sizes_after.values()) - metrics["disk"]["managed_before_bytes"],
            "managed_sizes_after": sizes_after,
            "model_after_bytes": model_snapshot()["size_bytes"],
        }
    )
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    print(json.dumps({
        "roads": osm["result"],
        "roads_count": roads_call["result_count"],
        "copernicus_stac": copernicus["result"],
        "sentinel_items": len(items) if copernicus_ok else 0,
        "accounted_live_mib": metrics["disk"]["accounted_live_mib"],
        "free_after_gib": metrics["disk"]["free_after_gib"],
    }, ensure_ascii=False, indent=2))
    return 0 if osm_ok and copernicus_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

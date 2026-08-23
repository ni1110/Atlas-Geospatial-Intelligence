"""Small, governed runtime used by the formal Geo Job entry point.

This module does not create a second architecture.  It supplies concrete local
operations to the existing SpecialistAgent/Harness boundary and deliberately
reuses the already accepted Gansu cache instead of downloading large data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from geoagent.orchestration.harness import AgentHandler, ToolInvoker
from geoagent.orchestration.models import ArtifactRef, GoalContract, TaskNode
from geoagent.orchestration.security import sha256_file
from renewable_site_agent.config import PATHS


def _safe_input_paths(input_artifacts: list[dict[str, Any]] | None) -> list[Path]:
    allowed_roots = tuple(root.resolve() for root in (PATHS.data, PATHS.cache, PATHS.artifacts, PATHS.temp))
    values: list[Path] = []
    for item in input_artifacts or []:
        raw = item.get("path") if isinstance(item, dict) else None
        if not raw:
            continue
        candidate = Path(str(raw)).resolve(strict=True)
        if not candidate.is_file() or not any(candidate == root or root in candidate.parents for root in allowed_roots):
            raise PermissionError(f"INPUT_ARTIFACT_OUTSIDE_GOVERNED_ROOTS: {candidate}")
        values.append(candidate)
    return values


def _load_geojson(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("type") not in {"Feature", "FeatureCollection"}:
        raise ValueError(f"Unsupported GeoJSON root in {path.name}")
    return value


def _features(value: dict[str, Any]) -> list[dict[str, Any]]:
    return value.get("features", []) if value.get("type") == "FeatureCollection" else [value]


def _geometry_operation(tool: str, paths: list[Path], constraints: dict[str, Any]) -> dict[str, Any]:
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    geo_paths = [path for path in paths if path.suffix.lower() in {".json", ".geojson"}]
    if not geo_paths:
        raise ValueError("A governed GeoJSON input_artifact is required")
    first = _features(_load_geojson(geo_paths[0]))
    geometries = [shape(feature["geometry"]) for feature in first]
    if tool == "buffer":
        distance = float(constraints.get("buffer_distance_m", constraints.get("distance_m", 500)))
        forward = Transformer.from_crs(4326, 32648, always_xy=True).transform
        backward = Transformer.from_crs(32648, 4326, always_xy=True).transform
        outputs = [transform(backward, transform(forward, geometry).buffer(distance)) for geometry in geometries]
    elif tool in {"intersect", "clip", "spatial_exclusion"}:
        if len(geo_paths) < 2:
            raise ValueError(f"{tool} requires two governed GeoJSON input_artifacts")
        masks = [shape(feature["geometry"]) for feature in _features(_load_geojson(geo_paths[1]))]
        mask = masks[0]
        for item in masks[1:]:
            mask = mask.union(item)
        outputs = [geometry.intersection(mask) if tool != "spatial_exclusion" else geometry.difference(mask) for geometry in geometries]
        outputs = [geometry for geometry in outputs if not geometry.is_empty]
    elif tool == "dissolve":
        output = geometries[0]
        for geometry in geometries[1:]:
            output = output.union(geometry)
        outputs = [output]
    else:
        outputs = geometries
    collection = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"operation": tool}, "geometry": mapping(item)} for item in outputs],
    }
    return {"operation": tool, "feature_count": len(outputs), "crs": "EPSG:4326", "geojson": collection}


def _cached_dem_operation(tool: str) -> dict[str, Any]:
    import numpy as np
    import rasterio

    path = PATHS.cache / "raster" / "dem" / "507d7b440fb73e62a18613cc.tif"
    if not path.is_file():
        raise FileNotFoundError("Accepted Gansu DEM cache is missing")
    with rasterio.open(path) as source:
        band = source.read(1, masked=True).astype("float64")
        valid = band.compressed()
        result: dict[str, Any] = {
            "operation": tool, "cache_path": str(path), "crs": str(source.crs),
            "shape": [source.height, source.width], "valid_pixels": int(valid.size),
            "elevation_min_m": float(valid.min()), "elevation_max_m": float(valid.max()),
            "elevation_mean_m": float(valid.mean()),
        }
        if tool in {"calculate_slope", "terrain_statistics", "calculate_aspect", "basic_hydrology"}:
            filled = band.filled(np.nan)
            gy, gx = np.gradient(filled, abs(source.transform.e), source.transform.a)
            slope = np.degrees(np.arctan(np.hypot(gx, gy)))
            result.update({"slope_mean_deg": float(np.nanmean(slope)), "slope_max_deg": float(np.nanmax(slope))})
        return result


def _provider_cache() -> dict[str, Any]:
    path = PATHS.artifacts / "evals" / "LIVE_DATA_PROVIDER_METRICS.json"
    if not path.is_file():
        raise FileNotFoundError("Accepted live provider metrics are missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    providers = []
    for item in value.get("providers", []):
        record = {"provider": item.get("provider"), "result": item.get("result"), "cache": item.get("cache")}
        if "STAC" in str(item.get("provider")) or "Computer" in str(item.get("provider")):
            details = item.get("details", {})
            record["item_metadata"] = {
                key: details.get(key)
                for key in ("endpoint", "collection", "date_range", "cloud_cover_lte", "item_count", "first_item_id", "asset_href_count", "asset_materialization", "fallback_reason")
                if key in details
            }
        providers.append(record)
    return {
        "source": str(path), "aoi": value.get("aoi"),
        "providers": providers,
        "sentinel_policy": "metadata_only_no_full_scene_download",
    }


def _live_stac_metadata(region_or_aoi: dict[str, Any] | str | None, constraints: dict[str, Any]) -> dict[str, Any]:
    from renewable_site_agent.data.manager import DataSourceManager

    if not isinstance(region_or_aoi, dict) or not isinstance(region_or_aoi.get("bbox"), list):
        raise ValueError("STAC metadata query requires an explicit bbox")
    query = {
        "bbox": region_or_aoi["bbox"],
        "date_range": constraints.get("stac_date_range", "2026-07-11/2026-08-10"),
        "cloud_cover": float(constraints.get("cloud_cover_lte", 20)),
        "limit": min(5, int(constraints.get("stac_item_limit", 5))),
    }
    result = DataSourceManager().acquire("sentinel", query)
    if result.status == "failed" or result.artifact is None:
        raise RuntimeError(f"STAC_METADATA_QUERY_FAILED: {result.message}")
    artifact = result.artifact
    items = artifact.metadata.get("items", [])
    compact = []
    for item in items[:5]:
        properties = item.get("properties", {})
        assets = item.get("assets", {})
        compact.append({
            "id": item.get("id"), "collection": item.get("collection"),
            "datetime": properties.get("datetime"), "cloud_cover": properties.get("eo:cloud_cover"),
            "geometry": item.get("geometry"), "bbox": item.get("bbox"),
            "asset_count": len(assets),
            "asset_refs": [{"key": key, "href": value.get("href")} for key, value in list(assets.items())[:8]],
        })
    return {
        "operation": "search_sentinel", "provider": artifact.provider,
        "source_url": artifact.source_url, "query": query, "item_count": len(items),
        "items": compact, "full_scene_downloaded": False,
        "cache_hit": bool(result.metrics.get("cache_hit")),
        "degraded": result.degraded, "fallback_provider": result.fallback_provider,
        "_execution_class": "CACHE_REUSED" if result.metrics.get("cache_hit") else "LIVE_NETWORK_EXECUTED",
    }


def formal_provider_status() -> dict[str, Any]:
    """Expose only already accepted cache availability to Goal/Planner context."""
    metrics = PATHS.artifacts / "evals" / "LIVE_DATA_PROVIDER_METRICS.json"
    dem = PATHS.cache / "raster" / "dem" / "507d7b440fb73e62a18613cc.tif"
    renewable = PATHS.artifacts / "evals" / "live-gansu-small-aoi" / "top_k_results-nodata-v1.json"
    return {
        "accepted_live_provider_metrics": str(metrics) if metrics.is_file() else None,
        "accepted_gansu_dem_cache": str(dem) if dem.is_file() else None,
        "accepted_renewable_composite": str(renewable) if renewable.is_file() else None,
        "sentinel_policy": "metadata_only_no_full_scene_download",
    }


def _renewable_cache() -> dict[str, Any]:
    path = PATHS.artifacts / "evals" / "live-gansu-small-aoi" / "top_k_results-nodata-v1.json"
    if not path.is_file():
        raise FileNotFoundError("Accepted renewable composite artifact is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value if isinstance(value, list) else [value]
    preview = []
    for row in rows[:3]:
        if isinstance(row, dict):
            preview.append({
                key: row.get(key)
                for key in ("rank", "candidate_id", "area_km2", "overall_score", "evidence_completeness_score", "key_strengths", "key_risks")
                if key in row
            })
    return {"source": str(path), "result_type": type(value).__name__, "candidate_count": len(rows), "preview": preview}


def build_formal_handlers(
    *, input_artifacts: list[dict[str, Any]] | None = None,
    constraints: dict[str, Any] | None = None,
    region_or_aoi: dict[str, Any] | str | None = None,
) -> dict[str, AgentHandler]:
    """Return role handlers that can execute only through ``ToolInvoker``."""
    paths = _safe_input_paths(input_artifacts)
    rules = constraints or {}

    def handler(task: TaskNode, contract: GoalContract, inputs: list[ArtifactRef], invoke: ToolInvoker) -> dict[str, Any]:
        outputs: list[dict[str, Any]] = []
        for tool_name in task.allowed_tools:
            if task.agent_role == "gis_analysis":
                if paths:
                    operation = lambda name=tool_name: _geometry_operation(name, paths, rules)
                    outputs.append(invoke(tool_name, operation, None, "OFFLINE_LOCAL_DATA_EXECUTED"))
                else:
                    operation = lambda name=tool_name: {"operation": name, "cached_domain_evidence": _renewable_cache()}
                    outputs.append(invoke(tool_name, operation, None, "CACHE_REUSED"))
            elif task.agent_role == "terrain":
                outputs.append(invoke(tool_name, lambda name=tool_name: _cached_dem_operation(name), "copernicus_glo30", "CACHE_REUSED"))
            elif task.agent_role in {"data", "remote_sensing"}:
                provider = "copernicus_dataspace_stac" if "sentinel" in tool_name or task.agent_role == "remote_sensing" else None
                if task.agent_role == "remote_sensing" and tool_name == "search_sentinel" and rules.get("metadata_live_query"):
                    outputs.append(invoke(tool_name, lambda: _live_stac_metadata(region_or_aoi, rules), provider, "LIVE_NETWORK_EXECUTED"))
                elif task.agent_role == "remote_sensing" and tool_name not in {"search_sentinel", "select_bands"}:
                    operation = lambda name=tool_name: {
                        "operation": name, "skipped": True,
                        "reason": "Freeze acceptance is metadata-only; no Sentinel full-scene or unverified band materialization",
                        "metadata": _provider_cache(),
                    }
                    outputs.append(invoke(tool_name, operation, provider, "SKIPPED"))
                else:
                    outputs.append(invoke(tool_name, _provider_cache, provider, "CACHE_REUSED"))
            elif task.agent_role == "renewable_siting":
                outputs.append(invoke(tool_name, _renewable_cache, None, "CACHE_REUSED"))
            elif task.agent_role == "map_report":
                operation = lambda: {
                    "operation": tool_name, "input_artifact_count": len(inputs),
                    "input_artifact_ids": [item.artifact_id for item in inputs],
                }
                outputs.append(invoke(tool_name, operation, None, "OFFLINE_LOCAL_DATA_EXECUTED"))
            else:
                outputs.append(invoke(tool_name, lambda: {"operation": tool_name}, None, "OFFLINE_LOCAL_DATA_EXECUTED"))
        return {
            "objective": task.objective, "contract_hash": contract.contract_hash,
            "planned_allowed_tools": task.allowed_tools, "actual_outputs": outputs,
            "source_input_artifact_refs": [
                {"path": str(path), "checksum_sha256": sha256_file(path)} for path in paths
            ],
            "input_artifact_ids": [item.artifact_id for item in inputs],
            "runtime": "formal_governed_runtime",
        }

    return {role: handler for role in ("data", "gis_analysis", "terrain", "remote_sensing", "renewable_siting", "map_report")}

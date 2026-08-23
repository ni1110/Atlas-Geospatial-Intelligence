"""Re-materialize only DEM after nodata fix and rerun cached-evidence GIS E2E."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone

from evals.run_live_data_provider_acceptance import (
    METRICS_PATH,
    REPORT_PATH,
    RUN_ROOT,
    TWO_GIB,
    artifact_of,
    managed_sizes,
    markdown_report,
    model_snapshot,
    nasa_value,
    raster_statistics,
    resource_score,
)
from renewable_site_agent.config import PATHS, apply_process_environment
from renewable_site_agent.data.manager import DataSourceManager
from renewable_site_agent.gis.engine import GISExecutor, feature_collection, overpass_to_geometries, write_geojson
from renewable_site_agent.gis.scoring import score_and_rank
from renewable_site_agent.schemas import AOI, SiteSelectionRequest


def main() -> int:
    apply_process_environment()
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    manager = DataSourceManager()
    gis = GISExecutor()
    bbox = metrics["aoi"]["bbox"]
    aoi_geometry = metrics["aoi"]["geometry"]
    original_aoi_fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"region": "Gansu", "acceptance_run": metrics["started_at"]},
            "geometry": aoi_geometry,
        }],
    }
    original_query = {
        "bbox": bbox,
        "aoi": original_aoi_fc,
        "acceptance_scope": "gansu-small-aoi-v1",
    }

    # New query/cache key only for the corrected DEM. This is the sole network
    # request in this recheck; every other acquire below resolves from cache.
    dem_query = {
        "bbox": bbox,
        "aoi": original_aoi_fc,
        "acceptance_scope": "gansu-small-aoi-nodata-v2",
        "materialize": True,
    }
    started = time.perf_counter()
    dem_result = manager.acquire("dem", dem_query)
    dem_latency = (time.perf_counter() - started) * 1000
    dem_artifact = artifact_of(dem_result)
    if not dem_artifact or not dem_artifact.local_path:
        raise RuntimeError(f"Corrected DEM acquisition failed: {dem_result.message}")
    dem_stats = raster_statistics(dem_artifact.local_path)
    slope_path = RUN_ROOT / "slope-nodata-v1.tif"
    slope_result = gis.calculate_slope(dem_artifact.local_path, str(slope_path), max_slope_deg=8.0)
    if slope_result.status == "failed":
        raise RuntimeError(slope_result.message)
    slope_stats = raster_statistics(str(slope_path))
    if dem_stats["nodata"] is None or dem_stats["min"] <= 0:
        raise RuntimeError(f"DEM nodata correction did not validate: nodata={dem_stats['nodata']} min={dem_stats['min']}")

    land_result = manager.acquire("land_cover", {**original_query, "materialize": True})
    water_result = manager.acquire("water", {**original_query, "materialize": True, "occurrence_threshold": 50})
    boundary_result = manager.acquire(
        "administrative_boundary",
        {"region": "\u7518\u8083", "acceptance_scope": "gansu-small-aoi-v1"},
    )
    power_result = manager.acquire("power_infrastructure", original_query)
    protected_result = manager.acquire("protected_areas", original_query)
    nasa_result = manager.acquire("energy_resource", {**original_query, "project_type": "solar"})
    road_cache_query = {
        "bbox": bbox,
        "aoi": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"region": "Gansu"}, "geometry": aoi_geometry}],
        },
        "acceptance_scope": "gansu-small-aoi-v1",
    }
    roads_result = manager.acquire("roads", road_cache_query)
    cached_results = [boundary_result, land_result, water_result, power_result, protected_result, nasa_result, roads_result]
    if any(result.metrics.get("cache_hit") is not True for result in cached_results):
        misses = [result.artifact.provider if artifact_of(result) else result.message for result in cached_results if result.metrics.get("cache_hit") is not True]
        raise RuntimeError(f"Non-DEM provider would require a repeated network request: {misses}")

    land_artifact = artifact_of(land_result)
    water_artifact = artifact_of(water_result)
    boundary_artifact = artifact_of(boundary_result)
    power_artifact = artifact_of(power_result)
    protected_artifact = artifact_of(protected_result)
    roads_artifact = artifact_of(roads_result)
    nasa_artifact = artifact_of(nasa_result)
    assert land_artifact and land_artifact.local_path
    assert water_artifact and water_artifact.local_path

    vector_exclusions = []
    for artifact in (protected_artifact,):
        if artifact:
            geometries = overpass_to_geometries(artifact.metadata.get("overpass_json", {}))
            if geometries:
                vector_exclusions.append(feature_collection(geometries))
    raster_exclusions = [
        {"path": str(slope_path), "mode": "gt", "threshold": 8.0},
        {"path": land_artifact.local_path, "mode": "in", "values": [50, 80, 90, 95]},
    ]
    if water_artifact.provider == "jrc_gsw":
        raster_exclusions.append({"path": water_artifact.local_path, "mode": "gte", "threshold": 50})
    else:
        geometries = overpass_to_geometries(water_artifact.metadata.get("overpass_json", {}))
        if geometries:
            vector_exclusions.append(feature_collection(geometries))
    suitable = gis.apply_spatial_exclusion(original_aoi_fc, vector_exclusions, raster_exclusions)
    if suitable.status == "failed":
        raise RuntimeError(suitable.message)
    candidates = gis.build_candidate_polygons(suitable.artifact, minimum_area_km2=0.05)
    if candidates.status == "failed" or not candidates.artifact.get("features"):
        raise RuntimeError(candidates.message or "No candidate survived corrected DEM")
    candidate_fc = candidates.artifact
    for artifact, property_name in (
        (power_artifact, "substation_distance_km"),
        (roads_artifact, "road_distance_km"),
    ):
        if artifact and artifact.metadata.get("overpass_json", {}).get("elements"):
            distance = gis.calculate_nearest_distance(
                candidate_fc,
                {"overpass_json": artifact.metadata["overpass_json"]},
                property_name,
            )
            if distance.status != "failed":
                candidate_fc = distance.artifact
    statistics_result = gis.calculate_candidate_statistics(
        candidate_fc,
        slope_raster_path=str(slope_path),
        land_cover_raster_path=land_artifact.local_path,
        land_cover_preferences={"10": 70, "20": 80, "30": 95, "40": 35, "60": 85},
        energy_resource_score=resource_score(nasa_value(nasa_artifact), "solar"),
    )
    if statistics_result.status == "failed":
        raise RuntimeError(statistics_result.message)
    candidate_fc = statistics_result.artifact
    validation = gis.validate_candidate_geometry(candidate_fc, original_aoi_fc, minimum_area_km2=0.05)
    if validation.status == "failed":
        raise RuntimeError(validation.message)
    provenance = [
        item for item in (
            boundary_artifact, dem_artifact, water_artifact, land_artifact, power_artifact,
            roads_artifact, protected_artifact, nasa_artifact,
        ) if item is not None
    ]
    request = SiteSelectionRequest(
        region="\u7518\u8083",
        aoi=AOI(bbox=tuple(bbox), geojson=original_aoi_fc),
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
        raise RuntimeError("Corrected E2E scoring returned no Top-K")
    candidates_path = RUN_ROOT / "candidates-nodata-v1.geojson"
    results_path = RUN_ROOT / "top_k_results-nodata-v1.json"
    write_geojson(candidates_path, candidate_fc)
    results_path.write_text(
        json.dumps([item.model_dump(mode="json") for item in ranked], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dem_provider = next(item for item in metrics["providers"] if item["provider"] == "Copernicus DEM GLO-30")
    initial_bytes = int(dem_provider["bytes"])
    initial_latency = float(dem_provider["latency_ms"])
    initial_elevation = dem_provider["details"].get("elevation")
    initial_slope = dem_provider["details"].get("slope")
    acceptance_dem_files = sorted((PATHS.cache / "raster" / "dem").glob("*.tif"))
    cumulative_dem_bytes = sum(path.stat().st_size for path in acceptance_dem_files)
    dem_provider.update(
        {
            "live_network": True,
            "data_fetched": True,
            "processing_succeeded": True,
            "bytes": cumulative_dem_bytes,
            "latency_ms": round(initial_latency + dem_latency, 3),
            "cache": "initial=MISS; nodata-corrected=MISS",
            "result": "PASS",
        }
    )
    dem_provider["details"].update(
        {
            "initial_nodata_issue": {
                "raster": initial_elevation,
                "slope": initial_slope,
                "finding": "Unmarked reprojection-edge zeros created false high slopes; result superseded.",
            },
            "elevation": dem_stats,
            "slope": slope_stats,
            "retained_aoi_bytes": dem_stats["landed_bytes"],
            "nodata_validation": {
                "nodata": dem_stats["nodata"],
                "valid_min_gt_zero": dem_stats["min"] > 0,
                "valid_fraction": dem_stats["valid_fraction"],
                "slope_max": slope_stats["max"],
                "supersedes_initial_artifact": True,
            },
            "acceptance_dem_files": [
                {"path": str(path), "bytes": path.stat().st_size}
                for path in acceptance_dem_files
            ],
            "targeted_recheck_latency_ms": dem_latency,
            "error": None,
        }
    )
    metrics["gis_e2e"] = {
        "result": "PASS",
        "message": "Live E2E reran with corrected DEM nodata; all non-DEM providers were cache hits.",
        "candidate_count": len(candidate_fc["features"]),
        "top_k_count": len(ranked),
        "candidates_path": str(candidates_path),
        "result_path": str(results_path),
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
        "non_dem_provider_cache_hits": True,
    }
    metrics["reliability"]["events"].append(
        {
            "dataset": "dem",
            "event": "targeted_recheck_after_nodata_validation_failure",
            "result": "PASS",
            "automatic_retry": False,
        }
    )
    nodata_change = "Initialized remote-raster mosaic gaps and metric reprojection edges as nodata, then versioned the DEM materialization key after live validation exposed zero-fill slope artifacts."
    if nodata_change not in metrics["code_changes"]:
        metrics["code_changes"].insert(-1, nodata_change)
    metrics["completed_at"] = datetime.now(timezone.utc).isoformat()
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
        "dem": "PASS",
        "dem_nodata": dem_stats["nodata"],
        "elevation_min_max_mean": [dem_stats["min"], dem_stats["max"], dem_stats["mean"]],
        "slope_min_max_mean": [slope_stats["min"], slope_stats["max"], slope_stats["mean"]],
        "gis_e2e": "PASS",
        "candidate_count": len(candidate_fc["features"]),
        "accounted_live_mib": metrics["disk"]["accounted_live_mib"],
        "free_after_gib": metrics["disk"]["free_after_gib"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

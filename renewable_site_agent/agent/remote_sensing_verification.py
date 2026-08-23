"""Closed Loop 4A: candidate sampling and remote-sensing verification package.

This module does not call a vision model yet. It prepares stable inputs for
remote-sensing verification:
1. sample representative candidate polygons from request-driven Agent outputs;
2. compute centroid, bbox, and area;
3. create verification task package;
4. create tile request manifest for later imagery acquisition / visual model use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from renewable_site_agent.application.create_job import RUNTIME_ROOT


AREA_CRS = "EPSG:32650"
WGS84 = "EPSG:4326"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _clean_gdf(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    gdf = gdf[gdf.geometry.notnull()]
    gdf = gdf[~gdf.geometry.is_empty]
    if len(gdf) == 0:
        return gdf
    gdf["geometry"] = gdf.geometry.buffer(0)
    gdf = gdf[gdf.geometry.notnull()]
    gdf = gdf[~gdf.geometry.is_empty]
    return gdf


def _load_candidate_gdf(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    gdf = _clean_gdf(gdf)

    if gdf.crs is None:
        # Most GeoJSON outputs in this project are WGS84-like. If CRS is missing,
        # set WGS84 to keep downstream metadata generation stable.
        gdf = gdf.set_crs(WGS84, allow_override=True)

    gdf = gdf.reset_index(drop=True)
    gdf["source_candidate_index"] = gdf.index.astype(int)
    return gdf


def _with_area_km2(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    work = gdf.copy()
    try:
        area_gdf = work.to_crs(AREA_CRS)
        work["area_km2"] = area_gdf.geometry.area / 1_000_000.0
    except Exception:
        # Fallback is not ideal, but prevents total failure if CRS metadata is bad.
        work["area_km2"] = work.geometry.area
    return work


def _select_samples(gdf: gpd.GeoDataFrame, top_k: int) -> gpd.GeoDataFrame:
    if len(gdf) == 0:
        return gdf

    work = _with_area_km2(gdf)
    work = work.sort_values("area_km2", ascending=False).reset_index(drop=True)

    if len(work) <= top_k:
        return work

    # Deterministic representative sampling:
    # 1 largest polygon, 1 median-area polygon, 1 smaller polygon when top_k=3.
    indexes = []
    if top_k >= 1:
        indexes.append(0)
    if top_k >= 2:
        indexes.append(len(work) // 2)
    if top_k >= 3:
        indexes.append(len(work) - 1)

    # If top_k > 3, fill with next largest polygons.
    for idx in range(len(work)):
        if len(indexes) >= top_k:
            break
        if idx not in indexes:
            indexes.append(idx)

    indexes = sorted(set(indexes))
    return work.iloc[indexes].copy().reset_index(drop=True)


def _safe_to_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        return gdf.set_crs(WGS84, allow_override=True)
    try:
        return gdf.to_crs(WGS84)
    except Exception:
        return gdf


def create_candidate_sampling_package(top_k_per_case: int = 3) -> dict[str, Any]:
    harness_path = RUNTIME_ROOT / "harness" / "request_driven_agent_harness_v1.json"
    harness = read_json(harness_path)

    case_results = harness.get("case_results", [])
    if not case_results:
        raise RuntimeError("No case_results found in request-driven harness report.")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    sample_records: list[dict[str, Any]] = []
    point_rows = []

    for case in case_results:
        final_candidate_path = case.get("final_candidate_path")
        if not final_candidate_path:
            continue

        candidate_path = Path(final_candidate_path)
        if not candidate_path.is_file():
            continue

        gdf = _load_candidate_gdf(candidate_path)
        sampled = _select_samples(gdf, top_k=top_k_per_case)
        sampled_wgs84 = _safe_to_wgs84(sampled)

        for local_idx, row in sampled_wgs84.iterrows():
            geom = row.geometry
            rep_point = geom.representative_point()
            bounds = geom.bounds

            sample_id = (
                f"rs_sample_{case.get('case_id')}_{int(local_idx):03d}"
            )

            area_km2 = float(row.get("area_km2", 0.0))

            record = {
                "sample_id": sample_id,
                "source_case_id": case.get("case_id"),
                "source_request_id": case.get("request_id"),
                "source_job_id": case.get("job_id"),
                "source_candidate_index": int(row.get("source_candidate_index", local_idx)),
                "source_final_candidate_path": final_candidate_path,
                "project_type": case.get("project_type"),
                "project_type_zh": case.get("project_type_zh"),
                "scope_kind": case.get("scope_kind"),
                "dynamic_execution_status": case.get("dynamic_execution_status"),
                "candidate_count_in_source": case.get("candidate_count"),
                "area_km2": area_km2,
                "centroid_wgs84": {
                    "lon": float(rep_point.x),
                    "lat": float(rep_point.y),
                },
                "bbox_wgs84": {
                    "min_lon": float(bounds[0]),
                    "min_lat": float(bounds[1]),
                    "max_lon": float(bounds[2]),
                    "max_lat": float(bounds[3]),
                },
                "remote_sensing_verification_status": "pending",
                "human_review_status": "not_reviewed",
                "verification_questions_zh": [
                    "该候选区是否以建设用地、裸地、低植被或可开发地表为主？",
                    "该候选区内是否存在明显大面积水体？",
                    "该候选区内是否存在明显密集建成区？",
                    "该候选区内是否存在明显山地陡坡或破碎地形？",
                    "该候选区是否需要人工复核？",
                ],
            }

            sample_records.append(record)

            point_rows.append(
                {
                    "sample_id": sample_id,
                    "source_case_id": case.get("case_id"),
                    "source_job_id": case.get("job_id"),
                    "project_type": case.get("project_type"),
                    "scope_kind": case.get("scope_kind"),
                    "area_km2": area_km2,
                    "verification_status": "pending",
                    "geometry": Point(rep_point.x, rep_point.y),
                }
            )

    if not sample_records:
        raise RuntimeError("No candidate samples were created.")

    remote_sensing_dir = RUNTIME_ROOT / "remote_sensing"
    package_path = remote_sensing_dir / "closed_loop_4a_candidate_sampling_package_v1.json"
    sample_points_path = remote_sensing_dir / "closed_loop_4a_sample_points.geojson"
    tile_manifest_path = remote_sensing_dir / "closed_loop_4a_tile_request_manifest_v1.json"

    sample_points_gdf = gpd.GeoDataFrame(point_rows, geometry="geometry", crs=WGS84)
    sample_points_path.parent.mkdir(parents=True, exist_ok=True)
    if sample_points_path.exists():
        sample_points_path.unlink()
    sample_points_gdf.to_file(sample_points_path, driver="GeoJSON")

    tile_tasks = []
    for record in sample_records:
        tile_tasks.append(
            {
                "sample_id": record["sample_id"],
                "source_job_id": record["source_job_id"],
                "project_type": record["project_type"],
                "scope_kind": record["scope_kind"],
                "centroid_wgs84": record["centroid_wgs84"],
                "bbox_wgs84": record["bbox_wgs84"],
                "suggested_zoom_levels": [12, 14, 16],
                "imagery_provider_status": "not_configured",
                "tile_download_status": "pending",
                "vision_verification_status": "pending",
            }
        )

    tile_manifest = {
        "tile_manifest_id": "closed_loop_4a_tile_request_manifest_v1",
        "created_at_utc": now,
        "tile_manifest_status": "created",
        "tile_manifest_status_zh": "遥感切片请求清单已创建",
        "imagery_provider_status": "not_configured",
        "imagery_provider_status_zh": "当前尚未配置遥感影像供应源或瓦片服务",
        "tile_task_count": len(tile_tasks),
        "tile_tasks": tile_tasks,
    }
    write_json(tile_manifest_path, tile_manifest)

    package = {
        "remote_sensing_package_id": "closed_loop_4a_candidate_sampling_package_v1",
        "created_at_utc": now,
        "package_status": "created",
        "package_status_zh": "闭环 4A 候选区抽样与遥感核验包已创建",
        "source_harness_path": str(harness_path),
        "sample_strategy": "deterministic_area_representative_sampling",
        "sample_strategy_zh": "按面积进行确定性代表性抽样",
        "top_k_per_case": top_k_per_case,
        "source_case_count": len(case_results),
        "sample_count": len(sample_records),
        "sample_points_geojson_path": str(sample_points_path),
        "tile_request_manifest_path": str(tile_manifest_path),
        "verification_stage": "remote_sensing_pending",
        "verification_stage_zh": "等待遥感影像切片和多模态核验",
        "sample_records": sample_records,
    }
    write_json(package_path, package)

    return {
        "closed_loop_4a_package_status": "created",
        "package_path": str(package_path),
        "sample_points_geojson_path": str(sample_points_path),
        "tile_request_manifest_path": str(tile_manifest_path),
        "source_case_count": len(case_results),
        "sample_count": len(sample_records),
        "verification_stage": package["verification_stage"],
    }


def validate_candidate_sampling_package() -> dict[str, Any]:
    package_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_candidate_sampling_package_v1.json"
    )
    package = read_json(package_path)

    sample_records = package.get("sample_records", [])
    if not sample_records:
        raise RuntimeError("Candidate sampling package has no samples.")

    sample_points_path = Path(package.get("sample_points_geojson_path", ""))
    tile_manifest_path = Path(package.get("tile_request_manifest_path", ""))

    if not sample_points_path.is_file():
        raise RuntimeError(f"Missing sample points GeoJSON: {sample_points_path}")
    if not tile_manifest_path.is_file():
        raise RuntimeError(f"Missing tile request manifest: {tile_manifest_path}")

    sample_ids = [record.get("sample_id") for record in sample_records]
    if len(sample_ids) != len(set(sample_ids)):
        raise RuntimeError("Duplicate sample_id found.")

    invalid_records = []
    for record in sample_records:
        centroid = record.get("centroid_wgs84", {})
        bbox = record.get("bbox_wgs84", {})
        area = record.get("area_km2")

        ok = (
            isinstance(area, (int, float))
            and area >= 0
            and isinstance(centroid.get("lon"), (int, float))
            and isinstance(centroid.get("lat"), (int, float))
            and -180 <= centroid["lon"] <= 180
            and -90 <= centroid["lat"] <= 90
            and all(key in bbox for key in ["min_lon", "min_lat", "max_lon", "max_lat"])
            and record.get("remote_sensing_verification_status") == "pending"
        )
        if not ok:
            invalid_records.append(record.get("sample_id"))

    if invalid_records:
        raise RuntimeError(f"Invalid sample records: {invalid_records}")

    tile_manifest = read_json(tile_manifest_path)
    if tile_manifest.get("tile_task_count") != len(sample_records):
        raise RuntimeError("tile_task_count does not match sample_count.")

    validation = {
        "closed_loop_4a_candidate_sampling_validation_status": "passed",
        "closed_loop_4a_status_zh": "闭环 4A 候选区抽样与遥感核验包验证通过",
        "package_path": str(package_path),
        "sample_points_geojson_path": str(sample_points_path),
        "tile_request_manifest_path": str(tile_manifest_path),
        "source_case_count": package.get("source_case_count"),
        "sample_count": len(sample_records),
        "tile_task_count": tile_manifest.get("tile_task_count"),
        "verification_stage": package.get("verification_stage"),
        "next_stage": "closed_loop_4b_remote_sensing_tile_acquisition",
        "next_stage_zh": "下一步进入闭环 4B：遥感影像切片获取或图像输入接入",
    }

    validation_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_candidate_sampling_validation_v1.json"
    )
    write_json(validation_path, validation)

    result = dict(validation)
    result["validation_report_path"] = str(validation_path)
    return result

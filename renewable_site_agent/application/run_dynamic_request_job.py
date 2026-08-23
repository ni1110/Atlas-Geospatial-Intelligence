"""Run a request-driven dynamic job with real GIS adapters."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd

from renewable_site_agent.agent.gis_request_adapters import (
    apply_protected_area_screening,
    apply_slope_screening,
    calculate_slope_raster,
    materialize_administrative_boundary,
    prepare_dem,
    prepare_protected_area_layer,
    vectorize_slope_mask,
)
from renewable_site_agent.application.create_job import RUNTIME_ROOT


BASELINE_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


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


def copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def count_geojson_features(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return len(gpd.read_file(path))
    except Exception:
        return None


def make_record(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_index": step["step_index"],
        "step_id": step["step_id"],
        "tool_id": step["tool_id"],
        "status": "completed",
        "status_zh": "已完成",
        "artifact_path": None,
        "candidate_count": None,
        "message_zh": "",
    }


def fail_record(record: dict[str, Any], message: str, blocked: bool = False) -> None:
    record["status"] = "blocked" if blocked else "failed"
    record["status_zh"] = "阻塞" if blocked else "失败"
    record["message_zh"] = message


def run_dynamic_request_job(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"
    intermediate_dir = job_dir / "intermediate"
    final_dir = job_dir / "final"
    preview_dir = job_dir / "map_preview"

    manifest = read_json(agent_dir / "request_job_manifest_v1.json")
    plan = read_json(agent_dir / "dynamic_agent_plan_v1.json")
    scope = read_json(agent_dir / "request_scope_v1.json")

    baseline_dir = RUNTIME_ROOT / "jobs" / BASELINE_JOB_ID

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    step_records: list[dict[str, Any]] = []

    current_candidate_path: Path | None = None
    boundary_path: Path | None = None
    protected_area_path: Path | None = None
    dem_path: Path | None = None
    slope_raster_path: Path | None = None
    slope_mask_path: Path | None = None

    failed = False
    failure_reason = None

    max_slope_deg = scope["parsed_request"].get("max_slope_deg")
    out_of_scope_count = int(manifest.get("requested_out_of_scope_constraint_count") or 0)

    for step in plan["planned_steps"]:
        tool_id = step["tool_id"]
        record = make_record(step)

        try:
            if tool_id == "load_administrative_boundary":
                boundary_path = intermediate_dir / "administrative_boundary.geojson"
                result = materialize_administrative_boundary(
                    baseline_dir=baseline_dir,
                    output_path=boundary_path,
                )
                current_candidate_path = boundary_path
                record["artifact_path"] = result["artifact_path"]
                record["candidate_count"] = result["candidate_count"]
                record["message_zh"] = "已加载并物化行政边界，作为当前初始候选区。"

            elif tool_id == "exclude_water_bodies":
                src = baseline_dir / "candidate_sites.geojson"
                dst = intermediate_dir / "candidate_sites_after_water_exclusion.geojson"
                if copy_if_exists(src, dst):
                    current_candidate_path = dst
                    record["artifact_path"] = str(dst)
                    record["candidate_count"] = count_geojson_features(dst)
                    record["message_zh"] = "已执行水体排除，生成非水体候选区。"
                else:
                    fail_record(record, f"缺少水体排除产物：{src}")
                    failed = True
                    failure_reason = record["message_zh"]

            elif tool_id == "prepare_protected_areas":
                protected_area_path = intermediate_dir / "protected_areas.geojson"
                result = prepare_protected_area_layer(
                    baseline_dir=baseline_dir,
                    output_path=protected_area_path,
                )
                record["artifact_path"] = result["artifact_path"]
                record["candidate_count"] = result["feature_count"]
                record["message_zh"] = "已准备保护区图层。"

            elif tool_id == "apply_protected_area_screening":
                if current_candidate_path is None:
                    fail_record(record, "当前没有可用于保护区筛选的候选区。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                elif protected_area_path is None:
                    fail_record(record, "保护区图层尚未准备。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                else:
                    dst = intermediate_dir / "candidate_sites_after_protected_area_screening.geojson"
                    result = apply_protected_area_screening(
                        candidate_path=current_candidate_path,
                        protected_area_path=protected_area_path,
                        output_path=dst,
                    )
                    current_candidate_path = dst
                    record["artifact_path"] = result["artifact_path"]
                    record["candidate_count"] = result["candidate_count"]
                    record["message_zh"] = "已执行真实保护区筛选。"

            elif tool_id == "prepare_terrain_dem":
                dem_path = intermediate_dir / "terrain" / "beijing_dem_utm50.tif"
                result = prepare_dem(
                    baseline_dir=baseline_dir,
                    output_path=dem_path,
                )
                record["artifact_path"] = result["artifact_path"]
                record["message_zh"] = "已准备 DEM 高程数据。"

            elif tool_id == "calculate_slope":
                if dem_path is None:
                    fail_record(record, "DEM 尚未准备，无法计算坡度。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                else:
                    slope_raster_path = RUNTIME_ROOT / "shared" / "terrain" / "beijing_slope_degrees.tif"
                    result = calculate_slope_raster(
                        dem_path=dem_path,
                        output_slope_path=slope_raster_path,
                    )
                    record["artifact_path"] = result["artifact_path"]
                    record["message_zh"] = "已根据 DEM 计算坡度栅格。"

            elif tool_id == "vectorize_slope_mask":
                if slope_raster_path is None:
                    fail_record(record, "坡度栅格尚未生成，无法矢量化坡度掩膜。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                elif max_slope_deg is None:
                    fail_record(record, "用户请求中没有有效坡度阈值。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                else:
                    safe_threshold = str(float(max_slope_deg)).replace(".", "p")
                    slope_mask_path = (
                        intermediate_dir
                        / "terrain"
                        / f"slope_mask_le_{safe_threshold}_deg_manifest.json"
                    )
                    result = vectorize_slope_mask(
                        slope_raster_path=slope_raster_path,
                        max_slope_deg=float(max_slope_deg),
                        output_mask_path=slope_mask_path,
                    )
                    record["artifact_path"] = result["artifact_path"]
                    record["candidate_count"] = result["mask_polygon_count"]
                    record["message_zh"] = f"已生成坡度 ≤ {max_slope_deg} 度的动态坡度掩膜。"

            elif tool_id == "apply_slope_screening":
                if current_candidate_path is None:
                    fail_record(record, "当前没有可用于坡度筛选的候选区。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                elif slope_mask_path is None:
                    fail_record(record, "坡度掩膜尚未生成。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]
                else:
                    dst = intermediate_dir / "candidate_sites_after_slope_screening.geojson"
                    result = apply_slope_screening(
                        candidate_path=current_candidate_path,
                        slope_mask_path=slope_mask_path,
                        output_path=dst,
                    )
                    current_candidate_path = dst
                    record["artifact_path"] = result["artifact_path"]
                    record["candidate_count"] = result["candidate_count"]
                    record["message_zh"] = f"已执行真实动态坡度筛选，阈值为 {max_slope_deg} 度。"

            elif tool_id == "finalize_request_scoped_candidates":
                if current_candidate_path and current_candidate_path.is_file():
                    dst = final_dir / "request_scoped_candidate_sites.geojson"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current_candidate_path, dst)
                    record["artifact_path"] = str(dst)
                    record["candidate_count"] = count_geojson_features(dst)
                    record["message_zh"] = "已封存当前请求范围内最终候选区。"
                else:
                    fail_record(record, "没有可封存的当前候选区产物。", blocked=True)
                    failed = True
                    failure_reason = record["message_zh"]

            elif tool_id == "build_map_preview":
                preview_path = preview_dir / "request_map_preview_manifest_v1.json"
                write_json(
                    preview_path,
                    {
                        "job_id": job_id,
                        "request_id": manifest["request_id"],
                        "preview_type": "manifest_only",
                        "candidate_artifact": str(final_dir / "request_scoped_candidate_sites.geojson"),
                        "created_at_utc": now,
                    },
                )
                record["artifact_path"] = str(preview_path)
                record["message_zh"] = "已生成请求地图预览 manifest。"

            elif tool_id == "validate_request_scoped_job":
                final_candidate_path = final_dir / "request_scoped_candidate_sites.geojson"
                candidate_count = count_geojson_features(final_candidate_path)
                validation_path = agent_dir / "request_scoped_job_validation_v1.json"
                validation_status = "passed" if candidate_count is not None and not failed else "failed"
                write_json(
                    validation_path,
                    {
                        "job_id": job_id,
                        "request_id": manifest["request_id"],
                        "validation_status": validation_status,
                        "candidate_count": candidate_count,
                        "final_candidate_path": str(final_candidate_path),
                        "created_at_utc": now,
                        "failure_reason": failure_reason,
                    },
                )
                record["artifact_path"] = str(validation_path)
                record["candidate_count"] = candidate_count
                if validation_status != "passed":
                    fail_record(record, "当前请求任务验证失败。")
                    failed = True
                    failure_reason = record["message_zh"]
                else:
                    record["message_zh"] = "已验证当前请求任务。"

            else:
                fail_record(record, f"未知工具：{tool_id}")
                failed = True
                failure_reason = record["message_zh"]

        except Exception as exc:
            fail_record(record, f"{type(exc).__name__}: {exc}")
            failed = True
            failure_reason = record["message_zh"]

        step_records.append(record)

        if record["status"] in {"failed", "blocked"}:
            break

    completed_count = sum(1 for item in step_records if item["status"] == "completed")
    failed_count = sum(1 for item in step_records if item["status"] in {"failed", "blocked"})
    pending_count = plan["planned_step_count"] - len(step_records)

    if failed:
        run_status = "failed_or_blocked"
        run_status_zh = "失败或阻塞"
    elif out_of_scope_count > 0:
        run_status = "completed_with_out_of_scope_constraints"
        run_status_zh = "当前可执行范围已完成，但存在当前版本暂不支持的请求约束"
    else:
        run_status = "completed"
        run_status_zh = "执行完成"

    final_candidate_path = final_dir / "request_scoped_candidate_sites.geojson"
    candidate_count = count_geojson_features(final_candidate_path)

    execution_record = {
        "job_id": job_id,
        "request_id": manifest["request_id"],
        "created_at_utc": now,
        "execution_mode": "dynamic_request_real_gis_adapter_runner",
        "run_status": run_status,
        "run_status_zh": run_status_zh,
        "project_type": manifest["project_type"],
        "project_type_zh": manifest["project_type_zh"],
        "scope_kind": manifest["scope_kind"],
        "planned_step_count": plan["planned_step_count"],
        "executed_step_count": len(step_records),
        "completed_step_count": completed_count,
        "failed_or_blocked_step_count": failed_count,
        "pending_step_count": pending_count,
        "candidate_count": candidate_count,
        "final_candidate_path": str(final_candidate_path),
        "failure_reason": failure_reason,
        "requested_out_of_scope_constraints": plan.get("requested_out_of_scope_constraints", []),
        "step_records": step_records,
    }

    execution_path = agent_dir / "dynamic_execution_run_v1.json"
    write_json(execution_path, execution_record)

    manifest["job_status"] = run_status
    manifest["job_status_zh"] = run_status_zh
    manifest["executed_at_utc"] = now
    manifest["dynamic_execution_path"] = str(execution_path)
    manifest["candidate_count"] = candidate_count
    write_json(agent_dir / "request_job_manifest_v1.json", manifest)

    return {
        "job_id": job_id,
        "request_id": manifest["request_id"],
        "dynamic_execution_status": run_status,
        "dynamic_execution_status_zh": run_status_zh,
        "project_type": manifest["project_type"],
        "scope_kind": manifest["scope_kind"],
        "planned_step_count": plan["planned_step_count"],
        "completed_step_count": completed_count,
        "failed_or_blocked_step_count": failed_count,
        "pending_step_count": pending_count,
        "candidate_count": candidate_count,
        "execution_path": str(execution_path),
        "final_candidate_path": str(final_candidate_path),
        "failure_reason": failure_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="Dynamic request job id.")
    args = parser.parse_args()

    result = run_dynamic_request_job(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

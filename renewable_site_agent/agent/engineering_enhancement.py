"""Closed Loop 3C engineering enhancement utilities.

This module creates three engineering artifacts:
1. context package: compressed state for long-running Agent work;
2. skill registry: reusable skill definitions;
3. internal tool schema: MCP-like internal tool protocol, not a full MCP server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


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


def create_context_package() -> dict[str, Any]:
    validation_path = (
        RUNTIME_ROOT
        / "request_driven_agent"
        / "closed_loop_3b_request_driven_validation_v1.json"
    )
    harness_path = RUNTIME_ROOT / "harness" / "request_driven_agent_harness_v1.json"

    validation = read_json(validation_path)
    harness = read_json(harness_path)

    cases = harness.get("case_results", [])

    compressed_cases = []
    for case in cases:
        compressed_cases.append(
            {
                "case_id": case.get("case_id"),
                "request_text": case.get("request_text"),
                "project_type": case.get("project_type"),
                "scope_kind": case.get("scope_kind"),
                "status": case.get("dynamic_execution_status"),
                "candidate_count": case.get("candidate_count"),
                "job_id": case.get("job_id"),
                "final_candidate_path": case.get("final_candidate_path"),
            }
        )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    context_package = {
        "context_package_id": "request_driven_agent_context_package_v1",
        "created_at_utc": now,
        "context_mode": "compressed_agent_state",
        "context_mode_zh": "压缩后的 Agent 状态上下文",
        "source_validation_path": str(validation_path),
        "source_harness_path": str(harness_path),
        "task_summary_zh": (
            "当前系统已完成闭环 3B：能够从自然语言新能源场站选址请求中解析项目类型、区域和约束，"
            "动态生成 scope、plan 和 job，并执行水体排除、保护区排除和任意数值坡度阈值筛选。"
        ),
        "capability_summary": {
            "project_types_covered": validation.get("project_types_covered", []),
            "scope_kinds_covered": validation.get("scope_kinds_covered", []),
            "supported_execution_capabilities": validation.get(
                "supported_execution_capabilities", []
            ),
            "request_driven_job_count": validation.get("request_driven_job_count"),
            "harness_passed_case_count": validation.get("passed_case_count"),
            "harness_failed_case_count": validation.get("failed_case_count"),
        },
        "known_boundaries": {
            "known_out_of_scope_constraints": validation.get(
                "known_out_of_scope_constraints", []
            ),
            "boundary_statement_zh": (
                "当前版本能够记录但不能真实执行风资源、电网接入成本、光照资源、基本农田和变电站距离。"
            ),
        },
        "compressed_case_memory": compressed_cases,
        "context_compression_policy": {
            "keep": [
                "request_text",
                "project_type",
                "scope_kind",
                "execution_status",
                "candidate_count",
                "job_id",
                "final_candidate_path",
            ],
            "drop": [
                "full geometry",
                "full step logs",
                "large intermediate raster/vector contents",
            ],
            "reason_zh": "长任务中只保留 Agent 决策和结果摘要，避免上下文膨胀。",
        },
        "next_context_use": [
            "loop_engineering",
            "skill_execution",
            "tool_schema_validation",
            "closed_loop_4_remote_sensing_verification",
        ],
    }

    output_path = RUNTIME_ROOT / "context" / "request_driven_agent_context_package_v1.json"
    write_json(output_path, context_package)

    return {
        "context_package_status": "created",
        "context_package_path": str(output_path),
        "compressed_case_count": len(compressed_cases),
    }


def create_skill_registry() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    skills = [
        {
            "skill_id": "request_understanding_skill",
            "skill_name_zh": "请求理解技能",
            "purpose_zh": "将自然语言新能源场站选址请求解析为结构化请求和 scope。",
            "input_contract": ["raw_request_text"],
            "output_contract": ["parsed_request", "scope_decision", "required_tool_ids"],
            "tools": ["parse_and_resolve"],
            "status": "available",
        },
        {
            "skill_id": "water_screening_skill",
            "skill_name_zh": "水体筛选技能",
            "purpose_zh": "排除水体区域，生成非水体候选区。",
            "input_contract": ["region_boundary"],
            "output_contract": ["candidate_sites_after_water_exclusion"],
            "tools": ["load_administrative_boundary", "exclude_water_bodies"],
            "status": "available",
        },
        {
            "skill_id": "protected_area_screening_skill",
            "skill_name_zh": "保护区筛选技能",
            "purpose_zh": "从当前候选区中排除保护区。",
            "input_contract": ["candidate_sites", "protected_area_layer"],
            "output_contract": ["candidate_sites_after_protected_area_screening"],
            "tools": ["prepare_protected_areas", "apply_protected_area_screening"],
            "status": "available",
        },
        {
            "skill_id": "dynamic_slope_screening_skill",
            "skill_name_zh": "动态坡度筛选技能",
            "purpose_zh": "根据用户给定的任意数值坡度阈值筛选候选区域。",
            "input_contract": ["candidate_sites", "dem_raster", "max_slope_deg"],
            "output_contract": ["candidate_sites_after_slope_screening"],
            "tools": [
                "prepare_terrain_dem",
                "calculate_slope",
                "vectorize_slope_mask",
                "apply_slope_screening",
            ],
            "parameter_support": {
                "max_slope_deg": "any numeric value satisfying 0 < max_slope_deg < 90"
            },
            "status": "available",
        },
        {
            "skill_id": "request_job_execution_skill",
            "skill_name_zh": "请求任务执行技能",
            "purpose_zh": "根据动态计划创建 request-specific job 并执行。",
            "input_contract": ["dynamic_agent_plan"],
            "output_contract": ["dynamic_execution_run", "final_candidate_sites"],
            "tools": ["create_dynamic_request_job", "run_dynamic_request_job"],
            "status": "available",
        },
        {
            "skill_id": "harness_validation_skill",
            "skill_name_zh": "Harness 验证技能",
            "purpose_zh": "批量运行多条自然语言请求，验证 Agent 不是固定 job demo。",
            "input_contract": ["harness_case_set"],
            "output_contract": ["harness_report", "case_summaries"],
            "tools": ["run_request_driven_harness", "validate_closed_loop_3b_request_driven"],
            "status": "available",
        },
    ]

    registry = {
        "skill_registry_id": "renewable_site_agent_skill_registry_v1",
        "created_at_utc": now,
        "registry_status": "available",
        "registry_status_zh": "技能登记表已创建",
        "skill_count": len(skills),
        "skills": skills,
    }

    output_path = RUNTIME_ROOT / "skills" / "renewable_site_agent_skill_registry_v1.json"
    write_json(output_path, registry)

    return {
        "skill_registry_status": "created",
        "skill_registry_path": str(output_path),
        "skill_count": len(skills),
    }


def create_internal_tool_schema_registry() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    tool_schemas = [
        {
            "tool_id": "load_administrative_boundary",
            "tool_group": "gis_boundary",
            "input_schema": {"region": "string"},
            "output_schema": {"administrative_boundary": "GeoJSON"},
            "artifact_contract": ["intermediate/administrative_boundary.geojson"],
            "failure_modes": ["missing_boundary_file", "invalid_geometry"],
            "retry_policy": "not_retry_without_data_fix",
        },
        {
            "tool_id": "exclude_water_bodies",
            "tool_group": "gis_screening",
            "input_schema": {"administrative_boundary": "GeoJSON"},
            "output_schema": {"candidate_sites_after_water_exclusion": "GeoJSON"},
            "artifact_contract": ["intermediate/candidate_sites_after_water_exclusion.geojson"],
            "failure_modes": ["missing_water_layer", "overlay_failure"],
            "retry_policy": "retry_after_data_or_geometry_fix",
        },
        {
            "tool_id": "prepare_protected_areas",
            "tool_group": "gis_layer",
            "input_schema": {"region": "string"},
            "output_schema": {"protected_area_layer": "GeoJSON"},
            "artifact_contract": ["intermediate/protected_areas.geojson"],
            "failure_modes": ["missing_protected_area_layer", "invalid_vector_format"],
            "retry_policy": "retry_after_data_fix",
        },
        {
            "tool_id": "apply_protected_area_screening",
            "tool_group": "gis_screening",
            "input_schema": {
                "candidate_sites": "GeoJSON",
                "protected_area_layer": "GeoJSON",
            },
            "output_schema": {"candidate_sites_after_protected_area_screening": "GeoJSON"},
            "artifact_contract": [
                "intermediate/candidate_sites_after_protected_area_screening.geojson"
            ],
            "failure_modes": ["crs_mismatch", "invalid_geometry", "overlay_failure"],
            "retry_policy": "retry_after_geometry_repair",
        },
        {
            "tool_id": "prepare_terrain_dem",
            "tool_group": "terrain",
            "input_schema": {"region": "string"},
            "output_schema": {"dem_raster": "GeoTIFF"},
            "artifact_contract": ["intermediate/terrain/beijing_dem_utm50.tif"],
            "failure_modes": ["missing_dem", "invalid_raster"],
            "retry_policy": "retry_after_data_fix",
        },
        {
            "tool_id": "calculate_slope",
            "tool_group": "terrain",
            "input_schema": {"dem_raster": "GeoTIFF"},
            "output_schema": {"slope_raster": "GeoTIFF"},
            "artifact_contract": ["intermediate/terrain/slope_degrees.tif"],
            "failure_modes": ["invalid_dem", "raster_compute_error"],
            "retry_policy": "retry_after_raster_fix",
        },
        {
            "tool_id": "vectorize_slope_mask",
            "tool_group": "terrain",
            "input_schema": {
                "slope_raster": "GeoTIFF",
                "max_slope_deg": "float, 0 < value < 90",
            },
            "output_schema": {"slope_screening_manifest": "JSON"},
            "artifact_contract": [
                "intermediate/terrain/slope_mask_le_<threshold>_deg_manifest.json"
            ],
            "failure_modes": ["invalid_threshold", "missing_slope_raster"],
            "retry_policy": "retry_after_parameter_or_raster_fix",
        },
        {
            "tool_id": "apply_slope_screening",
            "tool_group": "gis_screening",
            "input_schema": {
                "candidate_sites": "GeoJSON",
                "slope_screening_manifest": "JSON",
            },
            "output_schema": {"candidate_sites_after_slope_screening": "GeoJSON"},
            "artifact_contract": ["intermediate/candidate_sites_after_slope_screening.geojson"],
            "failure_modes": ["missing_candidate_sites", "raster_sampling_error"],
            "retry_policy": "retry_after_artifact_fix",
        },
        {
            "tool_id": "finalize_request_scoped_candidates",
            "tool_group": "request_output",
            "input_schema": {"latest_candidate_sites": "GeoJSON"},
            "output_schema": {"final_candidate_sites": "GeoJSON"},
            "artifact_contract": ["final/request_scoped_candidate_sites.geojson"],
            "failure_modes": ["missing_candidate_sites"],
            "retry_policy": "retry_after_upstream_fix",
        },
        {
            "tool_id": "validate_request_scoped_job",
            "tool_group": "validation",
            "input_schema": {"final_candidate_sites": "GeoJSON"},
            "output_schema": {"request_scoped_job_validation": "JSON"},
            "artifact_contract": ["agent/request_scoped_job_validation_v1.json"],
            "failure_modes": ["missing_final_candidate", "invalid_candidate_file"],
            "retry_policy": "not_retry_without_upstream_fix",
        },
    ]

    registry = {
        "tool_schema_registry_id": "internal_tool_schema_registry_v1",
        "created_at_utc": now,
        "schema_mode": "internal_mcp_like_tool_protocol",
        "schema_mode_zh": "内部类 MCP 工具协议，不是 MCP Server",
        "tool_count": len(tool_schemas),
        "tool_schemas": tool_schemas,
        "mcp_server_status": "not_implemented_in_closed_loop_3c",
        "mcp_server_status_zh": "闭环 3C 不实现完整 MCP Server，闭环 5 再升级。",
    }

    output_path = RUNTIME_ROOT / "tool_protocol" / "internal_tool_schema_registry_v1.json"
    write_json(output_path, registry)

    return {
        "tool_schema_registry_status": "created",
        "tool_schema_registry_path": str(output_path),
        "tool_count": len(tool_schemas),
    }


def create_engineering_enhancement_artifacts() -> dict[str, Any]:
    context = create_context_package()
    skills = create_skill_registry()
    tool_schema = create_internal_tool_schema_registry()

    return {
        "engineering_enhancement_status": "created",
        "context_package": context,
        "skill_registry": skills,
        "internal_tool_schema_registry": tool_schema,
    }

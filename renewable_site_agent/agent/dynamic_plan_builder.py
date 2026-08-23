"""Build a dynamic Agent plan from a request-scope JSON.

This planner converts the request-scope decision into an executable planning
record. It does not run GIS tools yet. It creates a plan that later runners can
execute step by step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOOL_METADATA = {
    "load_administrative_boundary": {
        "category": "gis_boundary",
        "category_zh": "行政边界工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "加载请求区域的行政边界。",
    },
    "exclude_water_bodies": {
        "category": "gis_exclusion",
        "category_zh": "GIS 排除工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "排除水体区域并生成非水体候选区。",
    },
    "prepare_terrain_dem": {
        "category": "terrain",
        "category_zh": "地形工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "准备 DEM 高程数据。",
    },
    "calculate_slope": {
        "category": "terrain",
        "category_zh": "地形工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "基于 DEM 计算坡度。",
    },
    "vectorize_slope_mask": {
        "category": "terrain",
        "category_zh": "地形工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "将坡度合格栅格矢量化。",
    },
    "apply_slope_screening": {
        "category": "gis_screening",
        "category_zh": "GIS 筛选工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "按坡度阈值筛选候选区域。",
    },
    "prepare_protected_areas": {
        "category": "gis_layer",
        "category_zh": "GIS 图层工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "准备保护区排除图层。",
    },
    "apply_protected_area_screening": {
        "category": "gis_screening",
        "category_zh": "GIS 筛选工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "排除保护区。",
    },
    "finalize_request_scoped_candidates": {
        "category": "request_output",
        "category_zh": "请求结果封存工具",
        "deterministic": True,
        "implementation_status": "planned_wrapper",
        "description_zh": "将当前请求范围内最后一步候选区封存为该请求的最终结果。",
    },
    "build_map_preview": {
        "category": "visualization",
        "category_zh": "地图预览工具",
        "deterministic": True,
        "implementation_status": "available",
        "description_zh": "生成地图预览图层。",
    },
    "validate_request_scoped_job": {
        "category": "validation",
        "category_zh": "结果验证工具",
        "deterministic": True,
        "implementation_status": "planned_wrapper",
        "description_zh": "验证当前请求范围内的结果和产物。",
    },
}


@dataclass(frozen=True)
class DynamicPlanBuildResult:
    request_id: str
    dynamic_plan_path: str
    plan_status: str
    plan_status_zh: str
    planned_step_count: int
    available_tool_step_count: int
    planned_wrapper_step_count: int
    requested_out_of_scope_constraint_count: int


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Scope file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_dynamic_plan(scope: dict[str, Any]) -> dict[str, Any]:
    request_id = scope["request_id"]
    parsed_request = scope["parsed_request"]
    required_tool_ids = scope["required_tool_ids"]

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    planned_steps: list[dict[str, Any]] = []
    for index, tool_id in enumerate(required_tool_ids, start=1):
        metadata = TOOL_METADATA.get(
            tool_id,
            {
                "category": "unknown",
                "category_zh": "未知工具",
                "deterministic": True,
                "implementation_status": "unknown",
                "description_zh": "工具尚未登记。",
            },
        )

        planned_steps.append(
            {
                "step_index": index,
                "step_id": f"step_{index:02d}_{tool_id}",
                "tool_id": tool_id,
                "tool_category": metadata["category"],
                "tool_category_zh": metadata["category_zh"],
                "deterministic": metadata["deterministic"],
                "implementation_status": metadata["implementation_status"],
                "description_zh": metadata["description_zh"],
                "execution_status": "planned_not_executed",
                "execution_status_zh": "已规划，尚未执行",
            }
        )

    available_tool_step_count = sum(
        1 for step in planned_steps if step["implementation_status"] == "available"
    )
    planned_wrapper_step_count = sum(
        1 for step in planned_steps if step["implementation_status"] == "planned_wrapper"
    )

    unsupported_tool_step_count = sum(
        1
        for step in planned_steps
        if step["implementation_status"] not in {"available", "planned_wrapper"}
    )

    if unsupported_tool_step_count:
        plan_status = "contains_unknown_tools"
        plan_status_zh = "包含未知工具，需要补充登记"
    elif scope["request_status"] == "partially_supported":
        plan_status = "planned_with_out_of_scope_constraints"
        plan_status_zh = "已规划，但存在当前版本暂不支持的请求约束"
    else:
        plan_status = "planned"
        plan_status_zh = "已规划"

    return {
        "request_id": request_id,
        "dynamic_plan_id": f"dynamic-agent-plan-{request_id}-v1",
        "dynamic_plan_version": 1,
        "created_at_utc": now,
        "plan_status": plan_status,
        "plan_status_zh": plan_status_zh,
        "planning_mode": "request_driven_deterministic_planner",
        "planning_mode_zh": "请求驱动的确定性规划器",
        "project_family": parsed_request.get("project_family"),
        "project_type": parsed_request.get("project_type"),
        "project_type_zh": parsed_request.get("project_type_zh"),
        "region": parsed_request.get("region"),
        "capacity_mw": parsed_request.get("capacity_mw"),
        "scope_kind": scope["scope_kind"],
        "scope_kind_zh": scope["scope_kind_zh"],
        "request_status": scope["request_status"],
        "request_status_zh": scope["request_status_zh"],
        "in_scope_constraints": scope["in_scope_constraints"],
        "requested_out_of_scope_constraints": scope[
            "requested_out_of_scope_constraints"
        ],
        "in_scope_constraint_count": scope["in_scope_constraint_count"],
        "requested_out_of_scope_constraint_count": scope[
            "requested_out_of_scope_constraint_count"
        ],
        "planned_steps": planned_steps,
        "planned_step_count": len(planned_steps),
        "available_tool_step_count": available_tool_step_count,
        "planned_wrapper_step_count": planned_wrapper_step_count,
        "unsupported_tool_step_count": unsupported_tool_step_count,
        "next_stage": "create_request_job_and_execute_plan",
        "next_stage_zh": "下一步创建请求任务并执行计划",
    }


def build_dynamic_plan_from_scope_file(scope_path: Path, output_dir: Path) -> DynamicPlanBuildResult:
    scope = read_json(scope_path)
    plan = build_dynamic_plan(scope)

    output_path = output_dir / f"{plan['request_id']}_dynamic_agent_plan.json"
    write_json(output_path, plan)

    return DynamicPlanBuildResult(
        request_id=plan["request_id"],
        dynamic_plan_path=str(output_path),
        plan_status=plan["plan_status"],
        plan_status_zh=plan["plan_status_zh"],
        planned_step_count=plan["planned_step_count"],
        available_tool_step_count=plan["available_tool_step_count"],
        planned_wrapper_step_count=plan["planned_wrapper_step_count"],
        requested_out_of_scope_constraint_count=plan[
            "requested_out_of_scope_constraint_count"
        ],
    )

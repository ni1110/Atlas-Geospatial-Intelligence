"""Deterministic planner for renewable-site agent workflows.

The planner assembles a machine-readable tool sequence from the Tool Registry.
It does not execute GIS operations and does not let an LLM invent geospatial
results.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from renewable_site_agent.agent.tool_registry import get_registered_tool


AVAILABLE_DATA_WORKFLOW_TOOL_IDS: tuple[str, ...] = (
    "parse_site_selection_request",
    "load_administrative_boundary",
    "exclude_water_bodies",
    "prepare_terrain_dem",
    "calculate_slope",
    "vectorize_slope_mask",
    "apply_slope_screening",
    "prepare_protected_areas",
    "apply_protected_area_screening",
    "finalize_available_data_candidates",
    "generate_available_data_report",
    "build_map_preview",
    "validate_available_data_job",
)


@dataclass(frozen=True)
class PlannedStep:
    """One planned tool call in an agent workflow."""

    sequence_index: int
    registered_tool_id: str
    registered_tool_module: str
    registered_tool_category: str
    deterministic: bool
    required_inputs: tuple[str, ...]
    produced_outputs: tuple[str, ...]
    execution_policy: str


@dataclass(frozen=True)
class AgentPlan:
    """A deterministic plan assembled from registered tools."""

    plan_kind: str
    planning_mode: str
    plan_status: str
    created_at_utc: str
    planned_steps: tuple[PlannedStep, ...]
    out_of_scope_constraints: tuple[dict[str, str], ...]
    safety_boundary: str


def build_available_data_agent_plan() -> AgentPlan:
    """Build the available-data GIS screening plan from the Tool Registry."""

    planned_steps: list[PlannedStep] = []

    for index, tool_id in enumerate(AVAILABLE_DATA_WORKFLOW_TOOL_IDS, start=1):
        tool = get_registered_tool(tool_id)
        planned_steps.append(
            PlannedStep(
                sequence_index=index,
                registered_tool_id=tool.tool_id,
                registered_tool_module=tool.module,
                registered_tool_category=tool.category,
                deterministic=tool.deterministic,
                required_inputs=tool.required_inputs,
                produced_outputs=tool.produced_outputs,
                execution_policy="execute_with_artifact_validation",
            )
        )

    return AgentPlan(
        plan_kind="available_data_gis_screening",
        planning_mode="deterministic_registry_planner",
        plan_status="planned_not_executed",
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        planned_steps=tuple(planned_steps),
        out_of_scope_constraints=(
            {
                "constraint": "max_distance_to_substation_km",
                "reason": "Substation-distance screening is not part of the current product scope.",
                "reason_zh": "变电站距离筛选不纳入当前版本产品范围。",
                "status": "out_of_scope_by_product_decision",
                "status_zh": "按产品决策不纳入当前版本范围",
            },
            {
                "constraint": "exclude_prime_farmland",
                "reason": "Prime farmland or land-use restriction screening is not part of the current product scope.",
                "reason_zh": "基本农田或土地利用限制筛选不纳入当前版本产品范围。",
                "status": "out_of_scope_by_product_decision",
                "status_zh": "按产品决策不纳入当前版本范围",
            },
        ),
        safety_boundary=(
            "The planner may assemble tool order from the registry, but GIS measurements "
            "must be produced by deterministic spatial tools and verified before reporting."
        ),
    )


def agent_plan_to_dict(plan: AgentPlan) -> dict[str, Any]:
    """Convert an AgentPlan to a JSON-serializable dictionary."""

    return {
        "plan_kind": plan.plan_kind,
        "planning_mode": plan.planning_mode,
        "plan_status": plan.plan_status,
        "created_at_utc": plan.created_at_utc,
        "planned_steps": [asdict(step) for step in plan.planned_steps],
        "out_of_scope_constraints": list(plan.out_of_scope_constraints),
        "safety_boundary": plan.safety_boundary,
    }


def main() -> None:
    plan = build_available_data_agent_plan()
    print(json.dumps(agent_plan_to_dict(plan), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

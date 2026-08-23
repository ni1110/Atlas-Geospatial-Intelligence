"""Validate a request-driven dynamic Agent plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.agent.dynamic_plan_builder import TOOL_METADATA


class DynamicAgentPlanValidationError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DynamicAgentPlanValidationError(f"Missing dynamic plan file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_dynamic_agent_plan(plan_path: str | Path) -> dict[str, Any]:
    path = Path(plan_path)
    plan = read_json(path)

    required_fields = [
        "request_id",
        "dynamic_plan_id",
        "dynamic_plan_version",
        "plan_status",
        "planning_mode",
        "project_family",
        "project_type",
        "region",
        "scope_kind",
        "request_status",
        "in_scope_constraints",
        "requested_out_of_scope_constraints",
        "planned_steps",
        "planned_step_count",
        "available_tool_step_count",
        "planned_wrapper_step_count",
        "unsupported_tool_step_count",
        "requested_out_of_scope_constraint_count",
    ]

    missing = [field for field in required_fields if field not in plan]
    if missing:
        raise DynamicAgentPlanValidationError(f"Missing fields: {missing}")

    steps = plan["planned_steps"]
    if not isinstance(steps, list) or not steps:
        raise DynamicAgentPlanValidationError("planned_steps must be a non-empty list.")

    if plan["planned_step_count"] != len(steps):
        raise DynamicAgentPlanValidationError("planned_step_count mismatch.")

    tool_ids = [step.get("tool_id") for step in steps]

    if len(tool_ids) != len(set(tool_ids)):
        raise DynamicAgentPlanValidationError("Duplicate tool_id found.")

    unknown_tools = [tool_id for tool_id in tool_ids if tool_id not in TOOL_METADATA]
    if unknown_tools:
        raise DynamicAgentPlanValidationError(f"Unknown tools: {unknown_tools}")

    if tool_ids[0] != "load_administrative_boundary":
        raise DynamicAgentPlanValidationError(
            "First tool must be load_administrative_boundary."
        )

    expected_tail = [
        "finalize_request_scoped_candidates",
        "build_map_preview",
        "validate_request_scoped_job",
    ]
    if tool_ids[-3:] != expected_tail:
        raise DynamicAgentPlanValidationError(
            f"Plan tail must be {expected_tail}, got {tool_ids[-3:]}"
        )

    available_count = sum(
        1 for step in steps if step.get("implementation_status") == "available"
    )
    wrapper_count = sum(
        1 for step in steps if step.get("implementation_status") == "planned_wrapper"
    )
    unsupported_count = sum(
        1
        for step in steps
        if step.get("implementation_status") not in {"available", "planned_wrapper"}
    )

    if available_count != plan["available_tool_step_count"]:
        raise DynamicAgentPlanValidationError("available_tool_step_count mismatch.")

    if wrapper_count != plan["planned_wrapper_step_count"]:
        raise DynamicAgentPlanValidationError("planned_wrapper_step_count mismatch.")

    if unsupported_count != plan["unsupported_tool_step_count"]:
        raise DynamicAgentPlanValidationError("unsupported_tool_step_count mismatch.")

    out_of_scope_count = len(plan["requested_out_of_scope_constraints"])
    if out_of_scope_count != plan["requested_out_of_scope_constraint_count"]:
        raise DynamicAgentPlanValidationError(
            "requested_out_of_scope_constraint_count mismatch."
        )

    if out_of_scope_count > 0:
        expected_status = "planned_with_out_of_scope_constraints"
    else:
        expected_status = "planned"

    if plan["plan_status"] != expected_status:
        raise DynamicAgentPlanValidationError(
            f"Unexpected plan_status: {plan['plan_status']}, expected {expected_status}"
        )

    return {
        "request_id": plan["request_id"],
        "dynamic_agent_plan_validation_status": "passed",
        "dynamic_plan_path": str(path),
        "plan_status": plan["plan_status"],
        "project_type": plan["project_type"],
        "project_type_zh": plan.get("project_type_zh"),
        "region": plan["region"],
        "scope_kind": plan["scope_kind"],
        "planned_step_count": plan["planned_step_count"],
        "available_tool_step_count": plan["available_tool_step_count"],
        "planned_wrapper_step_count": plan["planned_wrapper_step_count"],
        "requested_out_of_scope_constraint_count": plan[
            "requested_out_of_scope_constraint_count"
        ],
        "first_tool_id": tool_ids[0],
        "last_tool_id": tool_ids[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_path", help="Path to dynamic Agent plan JSON.")
    args = parser.parse_args()

    result = validate_dynamic_agent_plan(args.plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

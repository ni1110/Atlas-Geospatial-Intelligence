"""Validate persisted agent plan against the Tool Registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.agent.tool_registry import get_registered_tool


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class AgentPlanValidationError(RuntimeError):
    """Raised when a persisted agent plan is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AgentPlanValidationError(f"Missing agent plan file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_agent_plan(job_id: str) -> dict[str, Any]:
    agent_plan_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "agent_plan_v1.json"
    )

    plan = read_json(agent_plan_path)

    planned_steps = plan.get("planned_steps")
    if not isinstance(planned_steps, list) or not planned_steps:
        raise AgentPlanValidationError("planned_steps is missing or empty.")

    mismatches: list[dict[str, Any]] = []

    for step in planned_steps:
        tool_id = step.get("registered_tool_id")
        if not tool_id:
            mismatches.append({"error": "missing registered_tool_id", "step": step})
            continue

        try:
            tool = get_registered_tool(tool_id)
        except KeyError:
            mismatches.append(
                {
                    "registered_tool_id": tool_id,
                    "error": "tool id not found in Tool Registry",
                }
            )
            continue

        expected_pairs = {
            "registered_tool_module": tool.module,
            "registered_tool_category": tool.category,
            "deterministic": tool.deterministic,
            "required_inputs": list(tool.required_inputs),
            "produced_outputs": list(tool.produced_outputs),
        }

        for field, expected in expected_pairs.items():
            actual = step.get(field)
            if actual != expected:
                mismatches.append(
                    {
                        "registered_tool_id": tool_id,
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    blocked_constraints = plan.get("blocked_constraints", [])
    if not isinstance(blocked_constraints, list):
        raise AgentPlanValidationError("blocked_constraints must be a list.")

    out_of_scope_constraints = plan.get("out_of_scope_constraints", [])
    if not isinstance(out_of_scope_constraints, list):
        raise AgentPlanValidationError("out_of_scope_constraints must be a list.")

    if len(blocked_constraints) != 0:
        raise AgentPlanValidationError(
            f"blocked_constraints must be empty for the current scoped release: {len(blocked_constraints)}"
        )

    if len(out_of_scope_constraints) != 2:
        raise AgentPlanValidationError(
            f"Expected 2 out-of-scope constraints, got {len(out_of_scope_constraints)}."
        )

    if plan.get("plan_status") != "planned_not_executed":
        raise AgentPlanValidationError(
            f"Unexpected plan_status: {plan.get('plan_status')}"
        )

    if mismatches:
        raise AgentPlanValidationError(
            json.dumps(mismatches, ensure_ascii=False, indent=2)
        )

    return {
        "job_id": job_id,
        "agent_plan_validation_status": "passed",
        "agent_plan_path": str(agent_plan_path),
        "plan_id": plan.get("plan_id"),
        "plan_kind": plan.get("plan_kind"),
        "planning_mode": plan.get("planning_mode"),
        "plan_status": plan.get("plan_status"),
        "planned_step_count": len(planned_steps),
        "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "tool_binding_mismatch_count": len(mismatches),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_agent_plan(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

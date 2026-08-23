"""Validate execution-plan consistency against the tool registry and verification record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.agent.tool_registry import get_registered_tool


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class ExecutionPlanValidationError(RuntimeError):
    """Raised when an execution plan is inconsistent."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExecutionPlanValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_execution_plan(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    plan_path = agent_dir / "execution_plan_v1.json"
    verification_path = agent_dir / "execution_plan_v1.verification.json"

    plan = read_json(plan_path)
    verification = read_json(verification_path)

    steps = plan.get("completed_steps", [])
    if not isinstance(steps, list) or not steps:
        raise ExecutionPlanValidationError("completed_steps is missing or empty.")

    mismatches: list[dict[str, Any]] = []

    for step in steps:
        step_id = step.get("step_id")
        registered_tool_id = step.get("registered_tool_id")
        if not step_id or not registered_tool_id:
            mismatches.append(
                {
                    "step_id": step_id,
                    "error": "missing registered_tool_id",
                }
            )
            continue

        try:
            registered_tool = get_registered_tool(registered_tool_id)
        except KeyError:
            mismatches.append(
                {
                    "step_id": step_id,
                    "registered_tool_id": registered_tool_id,
                    "error": "registered tool not found in registry",
                }
            )
            continue

        expected = {
            "registered_tool_module": registered_tool.module,
            "registered_tool_category": registered_tool.category,
            "registered_tool_deterministic": registered_tool.deterministic,
        }

        for field, expected_value in expected.items():
            actual_value = step.get(field)
            if actual_value != expected_value:
                mismatches.append(
                    {
                        "step_id": step_id,
                        "registered_tool_id": registered_tool_id,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    final_result = plan.get("final_result", {})
    verification_summary = plan.get("verification_summary", {})
    checks = verification.get("checks", {})

    consistency_checks = {
        "plan_status": plan.get("plan_status"),
        "verification_status": verification.get("verification_status"),
        "step_count": len(steps),
        "candidate_count_matches": (
            final_result.get("candidate_count")
            == verification_summary.get("final_candidate_count")
            == checks.get("final_candidate_count")
        ),
        "preview_count_matches": (
            verification_summary.get("preview_candidate_count")
            == checks.get("preview_candidate_count")
        ),
        "water_overlap_matches": (
            verification_summary.get("water_overlap_km2")
            == checks.get("water_overlap_km2")
        ),
        "protected_overlap_matches": (
            verification_summary.get("protected_overlap_km2")
            == checks.get("protected_overlap_km2")
        ),
    }

    failed_consistency_checks = [
        name
        for name, value in consistency_checks.items()
        if name.endswith("_matches") and value is not True
    ]

    if mismatches or failed_consistency_checks:
        raise ExecutionPlanValidationError(
            json.dumps(
                {
                    "tool_mismatches": mismatches,
                    "failed_consistency_checks": failed_consistency_checks,
                    "consistency_checks": consistency_checks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    return {
        "job_id": job_id,
        "plan_validation_status": "passed",
        "plan_path": str(plan_path),
        "verification_path": str(verification_path),
        "step_count": len(steps),
        "tool_binding_mismatch_count": len(mismatches),
        "failed_consistency_checks": failed_consistency_checks,
        "candidate_count": final_result.get("candidate_count"),
        "verification_status": verification.get("verification_status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_execution_plan(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

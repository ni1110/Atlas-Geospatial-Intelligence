"""Validate PlanRun state record for one renewable-site job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class PlanRunValidationError(RuntimeError):
    """Raised when a PlanRun record is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PlanRunValidationError(f"Missing PlanRun file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_plan_run(job_id: str) -> dict[str, Any]:
    plan_run_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "plan_run_v1.json"
    )

    plan_run = read_json(plan_run_path)

    step_runs = plan_run.get("step_runs")
    if not isinstance(step_runs, list) or not step_runs:
        raise PlanRunValidationError("step_runs is missing or empty.")

    summary = plan_run.get("summary")
    if not isinstance(summary, dict):
        raise PlanRunValidationError("summary is missing or invalid.")

    status_counts = {
        "completed": sum(1 for step in step_runs if step.get("status") == "completed"),
        "failed": sum(1 for step in step_runs if step.get("status") == "failed"),
        "pending": sum(1 for step in step_runs if step.get("status") == "pending"),
        "skipped": sum(1 for step in step_runs if step.get("status") == "skipped"),
    }

    total_from_statuses = sum(status_counts.values())

    if total_from_statuses != len(step_runs):
        raise PlanRunValidationError(
            f"Unknown step status found. known_status_total={total_from_statuses}, step_count={len(step_runs)}"
        )

    expected_summary = {
        "total_step_count": len(step_runs),
        "completed_step_count": status_counts["completed"],
        "failed_step_count": status_counts["failed"],
        "pending_step_count": status_counts["pending"],
        "blocked_constraint_count": len(plan_run.get("blocked_constraints", [])),
        "out_of_scope_constraint_count": len(plan_run.get("out_of_scope_constraints", [])),
    }

    mismatches = []
    for field, expected in expected_summary.items():
        actual = summary.get(field)
        if actual != expected:
            mismatches.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                }
            )

    if mismatches:
        raise PlanRunValidationError(
            json.dumps(mismatches, ensure_ascii=False, indent=2)
        )

    allowed_run_statuses = {"completed", "completed_with_blocked_constraints"}
    if plan_run.get("run_status") not in allowed_run_statuses:
        raise PlanRunValidationError(
            f"Unexpected run_status: {plan_run.get('run_status')}"
        )

    if len(plan_run.get("blocked_constraints", [])) != 0:
        raise PlanRunValidationError(
            "blocked_constraints must be empty for the current scoped release."
        )

    if len(plan_run.get("out_of_scope_constraints", [])) != 2:
        raise PlanRunValidationError(
            "out_of_scope_constraints must contain 2 current scope exclusions."
        )

    if plan_run.get("agent_plan_validation_status") != "passed":
        raise PlanRunValidationError(
            f"Agent plan validation is not passed: {plan_run.get('agent_plan_validation_status')}"
        )

    if plan_run.get("execution_plan_validation_status") != "passed":
        raise PlanRunValidationError(
            f"Execution plan validation is not passed: {plan_run.get('execution_plan_validation_status')}"
        )

    return {
        "job_id": job_id,
        "plan_run_validation_status": "passed",
        "plan_run_path": str(plan_run_path),
        "plan_run_id": plan_run.get("plan_run_id"),
        "run_status": plan_run.get("run_status"),
        "total_step_count": summary["total_step_count"],
        "completed_step_count": summary["completed_step_count"],
        "failed_step_count": summary["failed_step_count"],
        "pending_step_count": summary["pending_step_count"],
        "blocked_constraint_count": summary["blocked_constraint_count"],
        "out_of_scope_constraint_count": summary["out_of_scope_constraint_count"],
        "unknown_status_count": len(step_runs) - total_from_statuses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_plan_run(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

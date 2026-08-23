"""Validate artifact-aware Agent executor run record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class AgentExecutorValidationError(RuntimeError):
    """Raised when an executor run record is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AgentExecutorValidationError(f"Missing executor run file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_agent_executor(job_id: str) -> dict[str, Any]:
    executor_run_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "executor_run_v1.json"
    )

    executor_run = read_json(executor_run_path)

    steps = executor_run.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AgentExecutorValidationError("steps is missing or empty.")

    summary = executor_run.get("summary")
    if not isinstance(summary, dict):
        raise AgentExecutorValidationError("summary is missing or invalid.")

    status_counts = {
        "completed": sum(1 for step in steps if step.get("status") == "completed"),
        "failed": sum(1 for step in steps if step.get("status") == "failed"),
        "pending": sum(1 for step in steps if step.get("status") == "pending"),
    }

    if sum(status_counts.values()) != len(steps):
        raise AgentExecutorValidationError("Unknown executor step status found.")

    expected_summary = {
        "total_step_count": len(steps),
        "completed_step_count": status_counts["completed"],
        "failed_step_count": status_counts["failed"],
        "pending_step_count": status_counts["pending"],
        "blocked_constraint_count": len(executor_run.get("blocked_constraints", [])),
        "out_of_scope_constraint_count": len(executor_run.get("out_of_scope_constraints", [])),
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
        raise AgentExecutorValidationError(
            json.dumps(mismatches, ensure_ascii=False, indent=2)
        )

    bad_completed_steps = []
    for step in steps:
        if step.get("status") != "completed":
            continue

        artifact_checks = step.get("artifact_checks", [])
        if not isinstance(artifact_checks, list):
            raise AgentExecutorValidationError(
                f"artifact_checks is invalid for {step.get('registered_tool_id')}"
            )

        missing_artifacts = [
            artifact
            for artifact in artifact_checks
            if artifact.get("exists") is not True
        ]

        if missing_artifacts:
            bad_completed_steps.append(
                {
                    "registered_tool_id": step.get("registered_tool_id"),
                    "missing_artifacts": missing_artifacts,
                }
            )

    if bad_completed_steps:
        raise AgentExecutorValidationError(
            json.dumps(bad_completed_steps, ensure_ascii=False, indent=2)
        )

    if executor_run.get("agent_plan_validation_status") != "passed":
        raise AgentExecutorValidationError(
            f"agent_plan_validation_status is not passed: {executor_run.get('agent_plan_validation_status')}"
        )

    if executor_run.get("execution_plan_validation_status") != "passed":
        raise AgentExecutorValidationError(
            f"execution_plan_validation_status is not passed: {executor_run.get('execution_plan_validation_status')}"
        )

    allowed_executor_statuses = {"completed", "completed_with_blocked_constraints"}
    if executor_run.get("executor_status") not in allowed_executor_statuses:
        raise AgentExecutorValidationError(
            f"Unexpected executor_status: {executor_run.get('executor_status')}"
        )

    if len(executor_run.get("blocked_constraints", [])) != 0:
        raise AgentExecutorValidationError(
            "blocked_constraints must be empty for the current scoped release."
        )

    if len(executor_run.get("out_of_scope_constraints", [])) != 2:
        raise AgentExecutorValidationError(
            "out_of_scope_constraints must contain 2 current scope exclusions."
        )

    return {
        "job_id": job_id,
        "agent_executor_validation_status": "passed",
        "executor_run_path": str(executor_run_path),
        "executor_status": executor_run.get("executor_status"),
        "executor_status_zh": executor_run.get("executor_status_zh"),
        "total_step_count": summary["total_step_count"],
        "completed_step_count": summary["completed_step_count"],
        "failed_step_count": summary["failed_step_count"],
        "pending_step_count": summary["pending_step_count"],
        "blocked_constraint_count": summary["blocked_constraint_count"],
        "out_of_scope_constraint_count": summary["out_of_scope_constraint_count"],
        "bad_completed_step_count": len(bad_completed_steps),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_agent_executor(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

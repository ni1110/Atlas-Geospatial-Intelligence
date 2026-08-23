"""Final validation for Closed Loop 3: Agent planning, execution, recovery, and audit control plane."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_control_plane import (
    validate_agent_control_plane,
)


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class ClosedLoop3FinalValidationError(RuntimeError):
    """Raised when Closed Loop 3 final validation fails."""


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def validate_required_artifacts(job_id: str) -> list[dict[str, Any]]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    required_artifacts = {
        "agent_plan": agent_dir / "agent_plan_v1.json",
        "execution_plan": agent_dir / "execution_plan_v1.json",
        "execution_plan_verification": agent_dir / "execution_plan_v1.verification.json",
        "plan_run": agent_dir / "plan_run_v1.json",
        "executor_run": agent_dir / "executor_run_v1.json",
        "scope_decision": agent_dir / "scope_decision_v1.json",
        "resume_policy": agent_dir / "resume_policy_v1.json",
        "dynamic_replanning_record": agent_dir / "dynamic_replanning_record_v1.json",
        "agent_state_summary": agent_dir / "agent_state_summary_v1.json",
        "deprecated_artifacts_record": agent_dir / "deprecated" / "deprecated_artifacts_v1.json",
        "deprecated_blocked_decision": agent_dir / "deprecated" / "blocked_decision_v1.deprecated.json",
        "failure_simulation_executor": agent_dir / "simulations" / "executor_run_failure_simulation_v1.json",
        "failure_simulation_resume_policy": agent_dir / "simulations" / "resume_policy_failure_simulation_v1.json",
        "final_candidates": job_dir / "final" / "available_data_screened_candidate_sites.geojson",
    }

    checks: list[dict[str, Any]] = []
    missing = []

    for name, path in required_artifacts.items():
        exists = path.is_file()
        checks.append(
            {
                "artifact_name": name,
                "path": str(path),
                "exists": exists,
            }
        )
        if not exists:
            missing.append({"artifact_name": name, "path": str(path)})

    if missing:
        raise ClosedLoop3FinalValidationError(
            "Missing required Closed Loop 3 artifacts: "
            + json.dumps(missing, ensure_ascii=False, indent=2)
        )

    return checks


def validate_closed_loop_3_final(job_id: str) -> dict[str, Any]:
    control_plane = validate_agent_control_plane(job_id)
    artifact_checks = validate_required_artifacts(job_id)

    expected_validation_keys = {
        "available_data_job",
        "execution_plan",
        "agent_plan",
        "plan_run",
        "agent_executor",
        "scope_decision",
        "resume_policy",
        "failure_simulation",
        "dynamic_replanning_record",
        "deprecated_artifacts",
        "agent_state_summary",
    }

    validation_statuses = control_plane.get("validation_statuses", {})
    missing_validation_keys = sorted(
        expected_validation_keys - set(validation_statuses.keys())
    )
    if missing_validation_keys:
        raise ClosedLoop3FinalValidationError(
            f"Missing validation keys: {missing_validation_keys}"
        )

    failed_validation_keys = [
        key
        for key in sorted(expected_validation_keys)
        if validation_statuses.get(key) != "passed"
    ]
    if failed_validation_keys:
        raise ClosedLoop3FinalValidationError(
            f"Validation keys not passed: {failed_validation_keys}"
        )

    if control_plane["agent_control_plane_validation_status"] != "passed":
        raise ClosedLoop3FinalValidationError("Agent control plane is not passed.")

    if control_plane["operational_status"] != "current_scope_completed":
        raise ClosedLoop3FinalValidationError(
            f"Unexpected operational_status: {control_plane['operational_status']}"
        )

    if control_plane["final_recommendation_allowed"] is not True:
        raise ClosedLoop3FinalValidationError(
            "final_recommendation_allowed must be true."
        )

    if control_plane["blocked_constraint_count"] != 0:
        raise ClosedLoop3FinalValidationError(
            "blocked_constraint_count must be 0."
        )

    if control_plane["out_of_scope_constraint_count"] != 2:
        raise ClosedLoop3FinalValidationError(
            "out_of_scope_constraint_count must be 2."
        )

    if control_plane["completed_step_count"] != 13:
        raise ClosedLoop3FinalValidationError(
            f"Expected 13 completed steps, got {control_plane['completed_step_count']}"
        )

    if control_plane["failed_step_count"] != 0:
        raise ClosedLoop3FinalValidationError("failed_step_count must be 0.")

    if control_plane["pending_step_count"] != 0:
        raise ClosedLoop3FinalValidationError("pending_step_count must be 0.")

    if control_plane["executor_status"] != "completed":
        raise ClosedLoop3FinalValidationError(
            f"Unexpected executor_status: {control_plane['executor_status']}"
        )

    if control_plane["bad_completed_step_count"] != 0:
        raise ClosedLoop3FinalValidationError(
            "bad_completed_step_count must be 0."
        )

    if control_plane["resume_policy_status"] != "no_resume_needed_current_scope_completed":
        raise ClosedLoop3FinalValidationError(
            f"Unexpected resume_policy_status: {control_plane['resume_policy_status']}"
        )

    if control_plane["failure_simulation_status"] != "passed":
        raise ClosedLoop3FinalValidationError("failure_simulation_status must be passed.")

    if control_plane["simulated_resume_policy_status"] != "resume_after_failure_fix":
        raise ClosedLoop3FinalValidationError(
            "Failure simulation must trigger resume_after_failure_fix."
        )

    if control_plane["real_system_status_after_simulation"] != "passed":
        raise ClosedLoop3FinalValidationError(
            "Real system must remain passed after failure simulation."
        )

    if control_plane["dynamic_replanning_needed_now"] is not False:
        raise ClosedLoop3FinalValidationError(
            "dynamic_replanning_needed_now must be false."
        )

    if control_plane["next_plan_version_if_scope_expands"] != 2:
        raise ClosedLoop3FinalValidationError(
            "next_plan_version_if_scope_expands must be 2."
        )

    if control_plane["deprecated_artifacts_status"] != "passed":
        raise ClosedLoop3FinalValidationError(
            "deprecated_artifacts_status must be passed."
        )

    if control_plane["old_blocked_decision_exists"] is not False:
        raise ClosedLoop3FinalValidationError(
            "old_blocked_decision_exists must be false."
        )

    if control_plane["polluted_active_control_file_count"] != 0:
        raise ClosedLoop3FinalValidationError(
            "polluted_active_control_file_count must be 0."
        )

    if control_plane["missing_artifact_count"] != 0:
        raise ClosedLoop3FinalValidationError(
            "missing_artifact_count must be 0."
        )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    final_result = {
        "job_id": job_id,
        "closed_loop_3_final_validation_status": "passed",
        "closed_loop_3_final_validation_status_zh": "闭环 3 最终验收通过",
        "created_at_utc": now,
        "control_plane_status": control_plane[
            "agent_control_plane_validation_status"
        ],
        "validation_statuses": validation_statuses,
        "candidate_count": control_plane["candidate_count"],
        "operational_status": control_plane["operational_status"],
        "operational_status_zh": control_plane["operational_status_zh"],
        "final_recommendation_allowed": control_plane[
            "final_recommendation_allowed"
        ],
        "final_recommendation_label": control_plane["final_recommendation_label"],
        "blocked_constraint_count": control_plane["blocked_constraint_count"],
        "out_of_scope_constraint_count": control_plane[
            "out_of_scope_constraint_count"
        ],
        "planned_step_count": control_plane["planned_step_count"],
        "completed_step_count": control_plane["completed_step_count"],
        "failed_step_count": control_plane["failed_step_count"],
        "pending_step_count": control_plane["pending_step_count"],
        "executor_status": control_plane["executor_status"],
        "resume_policy_status": control_plane["resume_policy_status"],
        "failure_simulation_status": control_plane["failure_simulation_status"],
        "dynamic_replanning_needed_now": control_plane[
            "dynamic_replanning_needed_now"
        ],
        "current_plan_version": control_plane["current_plan_version"],
        "next_plan_version_if_scope_expands": control_plane[
            "next_plan_version_if_scope_expands"
        ],
        "deprecated_artifacts_status": control_plane["deprecated_artifacts_status"],
        "missing_artifact_count": control_plane["missing_artifact_count"],
        "required_artifact_count": len(artifact_checks),
        "required_artifact_checks": artifact_checks,
    }

    final_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "closed_loop_3_final_validation_v1.json"
    )
    write_json_atomic(final_path, final_result)

    return {
        "job_id": job_id,
        "closed_loop_3_final_validation_status": "passed",
        "closed_loop_3_final_validation_status_zh": "闭环 3 最终验收通过",
        "closed_loop_3_final_validation_path": str(final_path),
        "candidate_count": final_result["candidate_count"],
        "operational_status": final_result["operational_status"],
        "operational_status_zh": final_result["operational_status_zh"],
        "final_recommendation_allowed": final_result["final_recommendation_allowed"],
        "blocked_constraint_count": final_result["blocked_constraint_count"],
        "out_of_scope_constraint_count": final_result["out_of_scope_constraint_count"],
        "completed_step_count": final_result["completed_step_count"],
        "failed_step_count": final_result["failed_step_count"],
        "pending_step_count": final_result["pending_step_count"],
        "failure_simulation_status": final_result["failure_simulation_status"],
        "dynamic_replanning_needed_now": final_result[
            "dynamic_replanning_needed_now"
        ],
        "deprecated_artifacts_status": final_result["deprecated_artifacts_status"],
        "required_artifact_count": final_result["required_artifact_count"],
        "missing_artifact_count": final_result["missing_artifact_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_closed_loop_3_final(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Validate the full scope-aware Agent control plane for one renewable-site job."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.application.validate_available_data_job import validate_job
from renewable_site_agent.application.validate_execution_plan import validate_execution_plan
from renewable_site_agent.application.validate_agent_plan import validate_agent_plan
from renewable_site_agent.application.validate_plan_run import validate_plan_run
from renewable_site_agent.application.validate_agent_executor import validate_agent_executor
from renewable_site_agent.application.validate_scope_decision import validate_scope_decision
from renewable_site_agent.application.validate_resume_policy import validate_resume_policy
from renewable_site_agent.application.validate_failure_simulation import (
    validate_failure_simulation,
)
from renewable_site_agent.application.validate_dynamic_replanning_record import (
    validate_dynamic_replanning_record,
)
from renewable_site_agent.application.validate_deprecated_artifacts import (
    validate_deprecated_artifacts,
)
from renewable_site_agent.application.validate_agent_state_summary import (
    validate_agent_state_summary,
)


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class AgentControlPlaneValidationError(RuntimeError):
    """Raised when the Agent control plane is inconsistent."""


def validate_agent_control_plane(job_id: str) -> dict:
    available_data_job = validate_job(job_id)
    execution_plan = validate_execution_plan(job_id)
    agent_plan = validate_agent_plan(job_id)
    plan_run = validate_plan_run(job_id)
    agent_executor = validate_agent_executor(job_id)
    scope_decision = validate_scope_decision(job_id)
    resume_policy = validate_resume_policy(job_id)
    failure_simulation = validate_failure_simulation(job_id, "prepare_terrain_dem")
    dynamic_replanning = validate_dynamic_replanning_record(job_id)
    deprecated_artifacts = validate_deprecated_artifacts(job_id)
    state_summary = validate_agent_state_summary(job_id)

    validation_statuses = {
        "available_data_job": available_data_job["validation_status"],
        "execution_plan": execution_plan["plan_validation_status"],
        "agent_plan": agent_plan["agent_plan_validation_status"],
        "plan_run": plan_run["plan_run_validation_status"],
        "agent_executor": agent_executor["agent_executor_validation_status"],
        "scope_decision": scope_decision["scope_decision_validation_status"],
        "resume_policy": resume_policy["resume_policy_validation_status"],
        "failure_simulation": failure_simulation[
            "failure_simulation_validation_status"
        ],
        "dynamic_replanning_record": dynamic_replanning[
            "dynamic_replanning_record_validation_status"
        ],
        "deprecated_artifacts": deprecated_artifacts[
            "deprecated_artifacts_validation_status"
        ],
        "agent_state_summary": state_summary[
            "agent_state_summary_validation_status"
        ],
    }

    failed_validations = [
        name for name, status in validation_statuses.items() if status != "passed"
    ]
    if failed_validations:
        raise AgentControlPlaneValidationError(
            f"Some validations failed: {failed_validations}"
        )

    if agent_plan["blocked_constraint_count"] != 0:
        raise AgentControlPlaneValidationError("Agent plan still has blocked constraints.")

    if plan_run["blocked_constraint_count"] != 0:
        raise AgentControlPlaneValidationError("PlanRun still has blocked constraints.")

    if agent_executor["blocked_constraint_count"] != 0:
        raise AgentControlPlaneValidationError("Executor still has blocked constraints.")

    if scope_decision["blocked_constraint_count"] != 0:
        raise AgentControlPlaneValidationError(
            "Scope decision still has blocked constraints."
        )

    out_of_scope_counts = {
        "agent_plan": agent_plan["out_of_scope_constraint_count"],
        "plan_run": plan_run["out_of_scope_constraint_count"],
        "agent_executor": agent_executor["out_of_scope_constraint_count"],
        "scope_decision": scope_decision["out_of_scope_constraint_count"],
        "resume_policy": resume_policy["out_of_scope_constraint_count"],
        "agent_state_summary": state_summary["out_of_scope_constraint_count"],
    }

    if len(set(out_of_scope_counts.values())) != 1:
        raise AgentControlPlaneValidationError(
            f"Out-of-scope counts are inconsistent: {out_of_scope_counts}"
        )

    if out_of_scope_counts["scope_decision"] != 2:
        raise AgentControlPlaneValidationError(
            f"Expected 2 out-of-scope constraints, got {out_of_scope_counts}"
        )

    if state_summary["final_recommendation_allowed"] is not True:
        raise AgentControlPlaneValidationError(
            "final_recommendation_allowed must be true for current scoped release."
        )

    if state_summary["missing_artifact_count"] != 0:
        raise AgentControlPlaneValidationError(
            f"Missing artifacts found: {state_summary['missing_artifact_count']}"
        )

    if agent_executor["bad_completed_step_count"] != 0:
        raise AgentControlPlaneValidationError(
            f"Bad completed steps found: {agent_executor['bad_completed_step_count']}"
        )

    if resume_policy["policy_status"] != "no_resume_needed_current_scope_completed":
        raise AgentControlPlaneValidationError(
            f"Unexpected resume policy status: {resume_policy['policy_status']}"
        )

    if resume_policy["automatic_resume_allowed"] is not False:
        raise AgentControlPlaneValidationError(
            "automatic_resume_allowed must be false when current scope is completed."
        )

    if failure_simulation["simulated_resume_policy_status"] != "resume_after_failure_fix":
        raise AgentControlPlaneValidationError(
            "Failure simulation did not trigger resume_after_failure_fix."
        )

    if failure_simulation["simulated_next_resume_step"] != "prepare_terrain_dem":
        raise AgentControlPlaneValidationError(
            "Failure simulation did not resume from prepare_terrain_dem."
        )

    if failure_simulation["real_system_status_after_simulation"] != "passed":
        raise AgentControlPlaneValidationError(
            "Real system was polluted by failure simulation."
        )

    if dynamic_replanning["dynamic_replanning_needed_now"] is not False:
        raise AgentControlPlaneValidationError(
            "Dynamic replanning should not be needed for the completed current scope."
        )

    if dynamic_replanning["current_plan_version"] != 1:
        raise AgentControlPlaneValidationError(
            "Current plan version must be 1."
        )

    if dynamic_replanning["next_plan_version_if_scope_expands"] != 2:
        raise AgentControlPlaneValidationError(
            "Next plan version must be 2 if scope expands."
        )

    if dynamic_replanning["future_scope_change_policy"] != "create_new_plan_version_not_resume_current_run":
        raise AgentControlPlaneValidationError(
            "Future scope change must create a new plan version."
        )

    if deprecated_artifacts["old_blocked_decision_exists"] is not False:
        raise AgentControlPlaneValidationError(
            "Old blocked_decision_v1.json still exists in active agent directory."
        )

    if deprecated_artifacts["polluted_active_control_file_count"] != 0:
        raise AgentControlPlaneValidationError(
            "Active control files still reference deprecated blocked decision."
        )

    return {
        "job_id": job_id,
        "agent_control_plane_validation_status": "passed",
        "validation_statuses": validation_statuses,
        "candidate_count": state_summary["candidate_count"],
        "operational_status": state_summary["operational_status"],
        "operational_status_zh": state_summary["operational_status_zh"],
        "final_recommendation_allowed": state_summary[
            "final_recommendation_allowed"
        ],
        "final_recommendation_label": state_summary[
            "final_recommendation_label"
        ],
        "blocked_constraint_count": state_summary["blocked_constraint_count"],
        "out_of_scope_constraint_count": state_summary[
            "out_of_scope_constraint_count"
        ],
        "planned_step_count": agent_plan["planned_step_count"],
        "completed_step_count": plan_run["completed_step_count"],
        "failed_step_count": plan_run["failed_step_count"],
        "pending_step_count": plan_run["pending_step_count"],
        "executor_status": agent_executor["executor_status"],
        "executor_status_zh": agent_executor["executor_status_zh"],
        "executor_failed_step_count": agent_executor["failed_step_count"],
        "executor_pending_step_count": agent_executor["pending_step_count"],
        "bad_completed_step_count": agent_executor["bad_completed_step_count"],
        "resume_policy_status": resume_policy["policy_status"],
        "resume_policy_status_zh": resume_policy["policy_status_zh"],
        "automatic_resume_allowed": resume_policy["automatic_resume_allowed"],
        "failure_simulation_status": failure_simulation[
            "failure_simulation_validation_status"
        ],
        "simulated_resume_policy_status": failure_simulation[
            "simulated_resume_policy_status"
        ],
        "simulated_next_resume_step": failure_simulation[
            "simulated_next_resume_step"
        ],
        "real_system_status_after_simulation": failure_simulation[
            "real_system_status_after_simulation"
        ],
        "dynamic_replanning_needed_now": dynamic_replanning[
            "dynamic_replanning_needed_now"
        ],
        "current_plan_version": dynamic_replanning["current_plan_version"],
        "next_plan_version_if_scope_expands": dynamic_replanning[
            "next_plan_version_if_scope_expands"
        ],
        "future_scope_change_policy": dynamic_replanning[
            "future_scope_change_policy"
        ],
        "deprecated_artifacts_status": deprecated_artifacts[
            "deprecated_artifacts_validation_status"
        ],
        "old_blocked_decision_exists": deprecated_artifacts[
            "old_blocked_decision_exists"
        ],
        "polluted_active_control_file_count": deprecated_artifacts[
            "polluted_active_control_file_count"
        ],
        "missing_artifact_count": state_summary["missing_artifact_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_agent_control_plane(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

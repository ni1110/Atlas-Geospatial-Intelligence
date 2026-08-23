"""Validate scope-aware resume and failure policy for one Agent job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class ResumePolicyValidationError(RuntimeError):
    """Raised when resume policy is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ResumePolicyValidationError(f"Missing resume policy file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_resume_policy(job_id: str) -> dict[str, Any]:
    policy_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "resume_policy_v1.json"
    )

    policy = read_json(policy_path)

    if policy.get("executor_validation_status") != "passed":
        raise ResumePolicyValidationError(
            f"executor_validation_status is not passed: {policy.get('executor_validation_status')}"
        )

    if policy.get("plan_run_validation_status") != "passed":
        raise ResumePolicyValidationError(
            f"plan_run_validation_status is not passed: {policy.get('plan_run_validation_status')}"
        )

    if policy.get("scope_decision_validation_status") != "passed":
        raise ResumePolicyValidationError(
            f"scope_decision_validation_status is not passed: {policy.get('scope_decision_validation_status')}"
        )

    if policy.get("failed_step_count") != len(policy.get("failed_steps", [])):
        raise ResumePolicyValidationError("failed_step_count does not match failed_steps length.")

    if policy.get("pending_step_count") != len(policy.get("pending_steps", [])):
        raise ResumePolicyValidationError("pending_step_count does not match pending_steps length.")

    if policy.get("blocked_constraint_count") != len(policy.get("blocked_constraints", [])):
        raise ResumePolicyValidationError(
            "blocked_constraint_count does not match blocked_constraints length."
        )

    if policy.get("out_of_scope_constraint_count") != len(policy.get("out_of_scope_constraints", [])):
        raise ResumePolicyValidationError(
            "out_of_scope_constraint_count does not match out_of_scope_constraints length."
        )

    if policy.get("failed_step_count") != 0:
        raise ResumePolicyValidationError("Current scoped release should not have failed steps.")

    if policy.get("pending_step_count") != 0:
        raise ResumePolicyValidationError("Current scoped release should not have pending steps.")

    if policy.get("blocked_constraint_count") != 0:
        raise ResumePolicyValidationError("Current scoped release should not have blocked constraints.")

    if policy.get("out_of_scope_constraint_count") != 2:
        raise ResumePolicyValidationError("Current scoped release should record 2 out-of-scope constraints.")

    if policy.get("policy_status") != "no_resume_needed_current_scope_completed":
        raise ResumePolicyValidationError(
            f"Unexpected policy_status: {policy.get('policy_status')}"
        )

    if policy.get("automatic_resume_allowed") is not False:
        raise ResumePolicyValidationError(
            "automatic_resume_allowed must be false when no resume is needed."
        )

    if policy.get("future_scope_change_policy") != "create_new_plan_version_not_resume_current_run":
        raise ResumePolicyValidationError(
            "future_scope_change_policy must require a new plan version."
        )

    return {
        "job_id": job_id,
        "resume_policy_validation_status": "passed",
        "resume_policy_path": str(policy_path),
        "policy_status": policy.get("policy_status"),
        "policy_status_zh": policy.get("policy_status_zh"),
        "automatic_resume_allowed": policy.get("automatic_resume_allowed"),
        "next_resume_step": policy.get("next_resume_step"),
        "failed_step_count": policy.get("failed_step_count"),
        "pending_step_count": policy.get("pending_step_count"),
        "blocked_constraint_count": policy.get("blocked_constraint_count"),
        "out_of_scope_constraint_count": policy.get("out_of_scope_constraint_count"),
        "future_scope_change_policy": policy.get("future_scope_change_policy"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_resume_policy(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

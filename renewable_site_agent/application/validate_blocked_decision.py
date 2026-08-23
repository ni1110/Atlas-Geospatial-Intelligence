"""Validate blocked-decision record for one renewable-site job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class BlockedDecisionValidationError(RuntimeError):
    """Raised when a blocked-decision record is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BlockedDecisionValidationError(f"Missing blocked decision file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_blocked_decision(job_id: str) -> dict[str, Any]:
    decision_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "blocked_decision_v1.json"
    )

    decision = read_json(decision_path)

    blocked_constraints = decision.get("blocked_constraints", [])
    required_actions = decision.get("required_actions", [])

    if decision.get("decision_status") != "human_or_data_action_required":
        raise BlockedDecisionValidationError(
            f"Unexpected decision_status: {decision.get('decision_status')}"
        )

    if decision.get("final_recommendation_allowed") is not False:
        raise BlockedDecisionValidationError(
            "final_recommendation_allowed must be false while blocked constraints exist."
        )

    if decision.get("preliminary_result_allowed") is not True:
        raise BlockedDecisionValidationError(
            "preliminary_result_allowed must be true for available-data preliminary result."
        )

    if decision.get("blocked_constraint_count") != len(blocked_constraints):
        raise BlockedDecisionValidationError(
            "blocked_constraint_count does not match blocked_constraints length."
        )

    if len(required_actions) != len(blocked_constraints):
        raise BlockedDecisionValidationError(
            "required_actions length must match blocked_constraints length."
        )

    if decision.get("plan_run_validation_status") != "passed":
        raise BlockedDecisionValidationError(
            f"plan_run_validation_status is not passed: {decision.get('plan_run_validation_status')}"
        )

    if decision.get("preliminary_result_label") != "preliminary_not_final_recommendation":
        raise BlockedDecisionValidationError(
            f"Unexpected preliminary_result_label: {decision.get('preliminary_result_label')}"
        )

    return {
        "job_id": job_id,
        "blocked_decision_validation_status": "passed",
        "decision_path": str(decision_path),
        "decision_status": decision.get("decision_status"),
        "decision_status_zh": decision.get("decision_status_zh"),
        "final_recommendation_allowed": decision.get("final_recommendation_allowed"),
        "preliminary_result_allowed": decision.get("preliminary_result_allowed"),
        "blocked_constraint_count": len(blocked_constraints),
        "required_action_count": len(required_actions),
        "preliminary_result_label": decision.get("preliminary_result_label"),
        "plan_run_validation_status": decision.get("plan_run_validation_status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_blocked_decision(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

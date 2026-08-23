"""Validate scope-aware agent-state summary for one renewable-site job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class AgentStateSummaryValidationError(RuntimeError):
    """Raised when an agent-state summary is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AgentStateSummaryValidationError(
            f"Missing agent-state summary file: {path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def validate_agent_state_summary(job_id: str) -> dict[str, Any]:
    summary_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "agent_state_summary_v1.json"
    )

    summary = read_json(summary_path)

    validation_statuses = summary.get("validation_statuses")
    if not isinstance(validation_statuses, dict):
        raise AgentStateSummaryValidationError("validation_statuses is missing.")

    failed_validations = [
        name
        for name, status in validation_statuses.items()
        if status != "passed"
    ]

    if failed_validations:
        raise AgentStateSummaryValidationError(
            f"Some validation statuses are not passed: {failed_validations}"
        )

    if summary.get("operational_status") != "current_scope_completed":
        raise AgentStateSummaryValidationError(
            f"Unexpected operational_status: {summary.get('operational_status')}"
        )

    if summary.get("final_recommendation_allowed") is not True:
        raise AgentStateSummaryValidationError(
            "final_recommendation_allowed must be true for the current scoped release."
        )

    if summary.get("blocked_constraint_count") != 0:
        raise AgentStateSummaryValidationError(
            f"blocked_constraint_count must be 0: {summary.get('blocked_constraint_count')}"
        )

    if summary.get("out_of_scope_constraint_count") != 2:
        raise AgentStateSummaryValidationError(
            f"out_of_scope_constraint_count must be 2: {summary.get('out_of_scope_constraint_count')}"
        )

    if summary.get("candidate_count") != 608:
        raise AgentStateSummaryValidationError(
            f"Unexpected candidate_count: {summary.get('candidate_count')}"
        )

    artifact_paths = summary.get("artifact_paths")
    if not isinstance(artifact_paths, dict):
        raise AgentStateSummaryValidationError("artifact_paths is missing.")

    missing_artifacts = []
    for name, path_text in artifact_paths.items():
        if not Path(path_text).is_file():
            missing_artifacts.append({"name": name, "path": path_text})

    if missing_artifacts:
        raise AgentStateSummaryValidationError(
            json.dumps(missing_artifacts, ensure_ascii=False, indent=2)
        )

    return {
        "job_id": job_id,
        "agent_state_summary_validation_status": "passed",
        "summary_path": str(summary_path),
        "operational_status": summary.get("operational_status"),
        "operational_status_zh": summary.get("operational_status_zh"),
        "final_recommendation_allowed": summary.get("final_recommendation_allowed"),
        "final_recommendation_label": summary.get("final_recommendation_label"),
        "candidate_count": summary.get("candidate_count"),
        "blocked_constraint_count": summary.get("blocked_constraint_count"),
        "out_of_scope_constraint_count": summary.get("out_of_scope_constraint_count"),
        "failed_validation_count": len(failed_validations),
        "missing_artifact_count": len(missing_artifacts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_agent_state_summary(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

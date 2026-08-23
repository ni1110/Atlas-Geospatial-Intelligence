"""Validate deprecated artifacts do not pollute the current scope-aware control plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class DeprecatedArtifactsValidationError(RuntimeError):
    """Raised when deprecated artifact state is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DeprecatedArtifactsValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_deprecated_artifacts(job_id: str) -> dict[str, Any]:
    agent_dir = RUNTIME_ROOT / "jobs" / job_id / "agent"
    deprecated_dir = agent_dir / "deprecated"

    old_blocked_path = agent_dir / "blocked_decision_v1.json"
    deprecated_blocked_path = deprecated_dir / "blocked_decision_v1.deprecated.json"
    deprecated_record_path = deprecated_dir / "deprecated_artifacts_v1.json"
    replacement_scope_path = agent_dir / "scope_decision_v1.json"

    if old_blocked_path.exists():
        raise DeprecatedArtifactsValidationError(
            f"Old blocked decision file still exists in active agent directory: {old_blocked_path}"
        )

    if not deprecated_blocked_path.is_file():
        raise DeprecatedArtifactsValidationError(
            f"Deprecated blocked decision archive is missing: {deprecated_blocked_path}"
        )

    if not deprecated_record_path.is_file():
        raise DeprecatedArtifactsValidationError(
            f"Deprecated artifacts record is missing: {deprecated_record_path}"
        )

    if not replacement_scope_path.is_file():
        raise DeprecatedArtifactsValidationError(
            f"Replacement scope decision is missing: {replacement_scope_path}"
        )

    record = read_json(deprecated_record_path)

    artifacts = record.get("deprecated_artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        raise DeprecatedArtifactsValidationError(
            "deprecated_artifacts must be a non-empty list."
        )

    blocked_entries = [
        item
        for item in artifacts
        if item.get("artifact_name") == "blocked_decision_v1.json"
    ]

    if len(blocked_entries) != 1:
        raise DeprecatedArtifactsValidationError(
            f"Expected one blocked_decision_v1.json deprecated entry, got {len(blocked_entries)}."
        )

    entry = blocked_entries[0]

    if entry.get("deprecated") is not True:
        raise DeprecatedArtifactsValidationError(
            "blocked_decision_v1.json entry must be marked deprecated."
        )

    if entry.get("must_not_use_for_current_status") is not True:
        raise DeprecatedArtifactsValidationError(
            "blocked_decision_v1.json must be marked as not usable for current status."
        )

    replacement = Path(entry.get("replacement_artifact", ""))
    if not replacement.is_file():
        raise DeprecatedArtifactsValidationError(
            f"Replacement artifact path is invalid: {replacement}"
        )

    current_scope_logic = record.get("current_scope_logic", {})
    if current_scope_logic.get("blocked_constraint_count") != 0:
        raise DeprecatedArtifactsValidationError(
            "current_scope_logic.blocked_constraint_count must be 0."
        )

    if current_scope_logic.get("out_of_scope_constraint_count") != 2:
        raise DeprecatedArtifactsValidationError(
            "current_scope_logic.out_of_scope_constraint_count must be 2."
        )

    active_control_files = [
        agent_dir / "agent_plan_v1.json",
        agent_dir / "plan_run_v1.json",
        agent_dir / "executor_run_v1.json",
        agent_dir / "scope_decision_v1.json",
        agent_dir / "resume_policy_v1.json",
        agent_dir / "dynamic_replanning_record_v1.json",
        agent_dir / "agent_state_summary_v1.json",
    ]

    polluted_files = []
    for path in active_control_files:
        if not path.is_file():
            raise DeprecatedArtifactsValidationError(f"Missing active control file: {path}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "blocked_decision" in text:
            polluted_files.append(str(path))

    if polluted_files:
        raise DeprecatedArtifactsValidationError(
            f"Active control files still reference blocked_decision: {polluted_files}"
        )

    return {
        "job_id": job_id,
        "deprecated_artifacts_validation_status": "passed",
        "deprecated_artifacts_record_path": str(deprecated_record_path),
        "old_blocked_decision_exists": old_blocked_path.exists(),
        "deprecated_blocked_decision_exists": deprecated_blocked_path.exists(),
        "replacement_scope_decision_exists": replacement_scope_path.exists(),
        "active_control_file_count": len(active_control_files),
        "polluted_active_control_file_count": len(polluted_files),
        "blocked_constraint_count": current_scope_logic.get("blocked_constraint_count"),
        "out_of_scope_constraint_count": current_scope_logic.get(
            "out_of_scope_constraint_count"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_deprecated_artifacts(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

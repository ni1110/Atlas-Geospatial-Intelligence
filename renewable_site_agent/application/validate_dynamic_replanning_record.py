"""Validate dynamic replanning record for future scope changes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class DynamicReplanningRecordValidationError(RuntimeError):
    """Raised when dynamic replanning record is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DynamicReplanningRecordValidationError(
            f"Missing dynamic replanning record file: {path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def validate_dynamic_replanning_record(job_id: str) -> dict[str, Any]:
    record_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "dynamic_replanning_record_v1.json"
    )

    record = read_json(record_path)

    if record.get("dynamic_replanning_needed_now") is not False:
        raise DynamicReplanningRecordValidationError(
            "dynamic_replanning_needed_now must be false for the completed current scope."
        )

    if record.get("current_plan_version") != 1:
        raise DynamicReplanningRecordValidationError(
            f"Expected current_plan_version=1, got {record.get('current_plan_version')}"
        )

    if record.get("next_plan_version_if_scope_expands") != 2:
        raise DynamicReplanningRecordValidationError(
            "next_plan_version_if_scope_expands must be 2."
        )

    if record.get("current_plan_must_not_be_overwritten") is not True:
        raise DynamicReplanningRecordValidationError(
            "current_plan_must_not_be_overwritten must be true."
        )

    if record.get("future_scope_change_policy") != "create_new_plan_version_not_resume_current_run":
        raise DynamicReplanningRecordValidationError(
            f"Unexpected future_scope_change_policy: {record.get('future_scope_change_policy')}"
        )

    if record.get("out_of_scope_constraint_count") != 2:
        raise DynamicReplanningRecordValidationError(
            f"Expected 2 out-of-scope constraints, got {record.get('out_of_scope_constraint_count')}"
        )

    if record.get("blocked_constraint_count") != 0:
        raise DynamicReplanningRecordValidationError(
            f"blocked_constraint_count must be 0, got {record.get('blocked_constraint_count')}"
        )

    if record.get("current_result_status") != "final_within_current_product_scope":
        raise DynamicReplanningRecordValidationError(
            f"Unexpected current_result_status: {record.get('current_result_status')}"
        )

    validation_dependencies = record.get("validation_dependencies", {})
    failed_dependencies = [
        name
        for name, status in validation_dependencies.items()
        if status != "passed"
    ]
    if failed_dependencies:
        raise DynamicReplanningRecordValidationError(
            f"Validation dependencies are not passed: {failed_dependencies}"
        )

    triggers = record.get("replanning_trigger_conditions", [])
    if not isinstance(triggers, list) or len(triggers) < 4:
        raise DynamicReplanningRecordValidationError(
            "replanning_trigger_conditions must contain at least 4 trigger definitions."
        )

    return {
        "job_id": job_id,
        "dynamic_replanning_record_validation_status": "passed",
        "dynamic_replanning_record_path": str(record_path),
        "dynamic_replanning_needed_now": record.get("dynamic_replanning_needed_now"),
        "current_plan_version": record.get("current_plan_version"),
        "next_plan_version_if_scope_expands": record.get(
            "next_plan_version_if_scope_expands"
        ),
        "current_result_status": record.get("current_result_status"),
        "future_scope_change_policy": record.get("future_scope_change_policy"),
        "out_of_scope_constraint_count": record.get("out_of_scope_constraint_count"),
        "blocked_constraint_count": record.get("blocked_constraint_count"),
        "replanning_trigger_count": len(triggers),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_dynamic_replanning_record(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

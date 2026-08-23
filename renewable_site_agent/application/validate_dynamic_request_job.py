"""Validate a request-driven dynamic job manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


class DynamicRequestJobValidationError(RuntimeError):
    """Raised when a dynamic request job is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DynamicRequestJobValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_dynamic_request_job(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"
    manifest_path = agent_dir / "request_job_manifest_v1.json"
    plan_path = agent_dir / "dynamic_agent_plan_v1.json"
    scope_path = agent_dir / "request_scope_v1.json"

    manifest = read_json(manifest_path)
    plan = read_json(plan_path)

    if manifest.get("job_id") != job_id:
        raise DynamicRequestJobValidationError("Manifest job_id does not match.")

    if manifest.get("request_id") != plan.get("request_id"):
        raise DynamicRequestJobValidationError("Manifest request_id does not match plan.")

    if manifest.get("job_status") != "created_not_executed":
        raise DynamicRequestJobValidationError(
            f"Unexpected job_status: {manifest.get('job_status')}"
        )

    if manifest.get("plan_validation_status") != "passed":
        raise DynamicRequestJobValidationError("Dynamic plan validation is not passed.")

    required_dirs = ["agent", "inputs", "intermediate", "final", "map_preview", "logs"]
    missing_dirs = [
        name for name in required_dirs if not (job_dir / name).is_dir()
    ]
    if missing_dirs:
        raise DynamicRequestJobValidationError(f"Missing job dirs: {missing_dirs}")

    if manifest.get("scope_copied") is True and not scope_path.is_file():
        raise DynamicRequestJobValidationError("scope_copied is true but scope file is missing.")

    if manifest.get("planned_step_count") != plan.get("planned_step_count"):
        raise DynamicRequestJobValidationError("planned_step_count mismatch.")

    return {
        "job_id": job_id,
        "request_id": manifest.get("request_id"),
        "dynamic_request_job_validation_status": "passed",
        "job_dir": str(job_dir),
        "manifest_path": str(manifest_path),
        "job_status": manifest.get("job_status"),
        "job_status_zh": manifest.get("job_status_zh"),
        "project_type": manifest.get("project_type"),
        "project_type_zh": manifest.get("project_type_zh"),
        "scope_kind": manifest.get("scope_kind"),
        "plan_status": manifest.get("plan_status"),
        "planned_step_count": manifest.get("planned_step_count"),
        "requested_out_of_scope_constraint_count": manifest.get(
            "requested_out_of_scope_constraint_count"
        ),
        "scope_copied": manifest.get("scope_copied"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="Dynamic request job id.")
    args = parser.parse_args()

    result = validate_dynamic_request_job(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

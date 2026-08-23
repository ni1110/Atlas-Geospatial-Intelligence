"""Create a new request-driven job from a dynamic Agent plan."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_dynamic_agent_plan import (
    validate_dynamic_agent_plan,
)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_dynamic_request_job(plan_path: str | Path) -> dict[str, Any]:
    source_plan_path = Path(plan_path)
    plan_validation = validate_dynamic_agent_plan(source_plan_path)
    plan = read_json(source_plan_path)

    request_id = plan["request_id"]
    job_id = f"request-job-{uuid.uuid4()}"
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    for subdir in ["agent", "inputs", "intermediate", "final", "map_preview", "logs"]:
        (job_dir / subdir).mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    copied_plan_path = agent_dir / "dynamic_agent_plan_v1.json"
    shutil.copy2(source_plan_path, copied_plan_path)

    source_scope_path = RUNTIME_ROOT / "request_scopes" / f"{request_id}_scope.json"
    copied_scope_path = agent_dir / "request_scope_v1.json"
    if source_scope_path.is_file():
        shutil.copy2(source_scope_path, copied_scope_path)
        scope_copied = True
    else:
        scope_copied = False

    manifest = {
        "job_id": job_id,
        "request_id": request_id,
        "job_kind": "request_driven_renewable_site_selection",
        "job_kind_zh": "请求驱动的新能源场站选址任务",
        "created_at_utc": now,
        "job_status": "created_not_executed",
        "job_status_zh": "任务已创建，尚未执行",
        "project_family": plan.get("project_family"),
        "project_type": plan.get("project_type"),
        "project_type_zh": plan.get("project_type_zh"),
        "region": plan.get("region"),
        "capacity_mw": plan.get("capacity_mw"),
        "scope_kind": plan.get("scope_kind"),
        "scope_kind_zh": plan.get("scope_kind_zh"),
        "request_status": plan.get("request_status"),
        "request_status_zh": plan.get("request_status_zh"),
        "plan_status": plan.get("plan_status"),
        "plan_status_zh": plan.get("plan_status_zh"),
        "planned_step_count": plan.get("planned_step_count"),
        "available_tool_step_count": plan.get("available_tool_step_count"),
        "planned_wrapper_step_count": plan.get("planned_wrapper_step_count"),
        "requested_out_of_scope_constraint_count": plan.get(
            "requested_out_of_scope_constraint_count"
        ),
        "source_dynamic_plan_path": str(source_plan_path),
        "copied_dynamic_plan_path": str(copied_plan_path),
        "source_scope_path": str(source_scope_path),
        "copied_scope_path": str(copied_scope_path),
        "scope_copied": scope_copied,
        "plan_validation_status": plan_validation[
            "dynamic_agent_plan_validation_status"
        ],
        "next_stage": "execute_dynamic_request_job",
        "next_stage_zh": "下一步执行动态请求任务",
    }

    manifest_path = agent_dir / "request_job_manifest_v1.json"
    write_json(manifest_path, manifest)

    return {
        "job_id": job_id,
        "request_id": request_id,
        "job_dir": str(job_dir),
        "manifest_path": str(manifest_path),
        "job_status": manifest["job_status"],
        "job_status_zh": manifest["job_status_zh"],
        "project_type": manifest["project_type"],
        "project_type_zh": manifest["project_type_zh"],
        "scope_kind": manifest["scope_kind"],
        "plan_status": manifest["plan_status"],
        "planned_step_count": manifest["planned_step_count"],
        "requested_out_of_scope_constraint_count": manifest[
            "requested_out_of_scope_constraint_count"
        ],
        "plan_validation_status": manifest["plan_validation_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_path", help="Path to dynamic Agent plan JSON.")
    args = parser.parse_args()

    result = create_dynamic_request_job(args.plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Create a persisted agent plan for one renewable-site job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.agent.planner import (
    agent_plan_to_dict,
    build_available_data_agent_plan,
)


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so partially written plan files are avoided."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def create_agent_plan(job_id: str) -> dict[str, Any]:
    """Create and persist the deterministic available-data agent plan."""

    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    plan = build_available_data_agent_plan()
    payload = agent_plan_to_dict(plan)
    payload["job_id"] = job_id
    payload["plan_id"] = f"agent-plan-{job_id}-v1"
    payload["plan_version"] = 1
    payload.setdefault("blocked_constraints", [])
    payload.setdefault("out_of_scope_constraints", [])

    plan_path = agent_dir / "agent_plan_v1.json"
    write_json_atomic(plan_path, payload)

    return {
        "job_id": job_id,
        "agent_plan_path": str(plan_path),
        "plan_id": payload["plan_id"],
        "plan_version": payload["plan_version"],
        "plan_kind": payload["plan_kind"],
        "planning_mode": payload["planning_mode"],
        "plan_status": payload["plan_status"],
        "planned_step_count": len(payload["planned_steps"]),
        "blocked_constraint_count": len(payload.get("blocked_constraints", [])),
        "out_of_scope_constraint_count": len(payload.get("out_of_scope_constraints", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_agent_plan(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

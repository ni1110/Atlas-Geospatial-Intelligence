"""Create a PlanRun state record from a persisted agent plan.

PlanRun records step-level execution states for audit, resume, and future
executor integration. This first version does not rerun GIS tools; it records
the current available-data workflow state from existing artifacts.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_plan import validate_agent_plan
from renewable_site_agent.application.validate_execution_plan import validate_execution_plan


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


COMPLETED_AVAILABLE_DATA_TOOL_IDS = {
    "parse_site_selection_request",
    "load_administrative_boundary",
    "exclude_water_bodies",
    "prepare_terrain_dem",
    "calculate_slope",
    "vectorize_slope_mask",
    "apply_slope_screening",
    "prepare_protected_areas",
    "apply_protected_area_screening",
    "finalize_available_data_candidates",
    "generate_available_data_report",
    "build_map_preview",
    "validate_available_data_job",
}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def create_plan_run(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"
    agent_plan_path = agent_dir / "agent_plan_v1.json"

    plan = read_json(agent_plan_path)

    agent_plan_validation = validate_agent_plan(job_id)
    execution_plan_validation = validate_execution_plan(job_id)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    step_runs: list[dict[str, Any]] = []

    for step in plan["planned_steps"]:
        tool_id = step["registered_tool_id"]

        if tool_id in COMPLETED_AVAILABLE_DATA_TOOL_IDS:
            status = "completed"
            reason = "Existing available-data artifact has been verified."
        else:
            status = "pending"
            reason = "No verified artifact has been recorded yet."

        step_runs.append(
            {
                "sequence_index": step["sequence_index"],
                "registered_tool_id": tool_id,
                "registered_tool_module": step["registered_tool_module"],
                "status": status,
                "status_reason": reason,
                "execution_policy": step["execution_policy"],
                "deterministic": step["deterministic"],
                "started_at_utc": None,
                "finished_at_utc": now if status == "completed" else None,
                "error": None,
            }
        )

    blocked_constraints = []
    for item in plan.get("blocked_constraints", []):
        blocked_constraints.append(
            {
                **item,
                "run_status": "blocked",
                "requires_human_or_data_action": True,
            }
        )

    out_of_scope_constraints = []
    for item in plan.get("out_of_scope_constraints", []):
        out_of_scope_constraints.append(
            {
                **item,
                "run_status": "out_of_scope",
                "requires_human_or_data_action": False,
            }
        )

    plan_run = {
        "job_id": job_id,
        "plan_id": plan["plan_id"],
        "plan_run_id": f"plan-run-{job_id}-v1",
        "plan_run_version": 1,
        "created_at_utc": now,
        "run_status": (
            "completed_with_blocked_constraints"
            if blocked_constraints
            else "completed"
        ),
        "status_meaning": (
            "All current-scope deterministic steps are verified as completed. "
            "Out-of-scope constraints are recorded for transparency but do not block "
            "the current product-scope recommendation."
        ),
        "agent_plan_validation_status": agent_plan_validation["agent_plan_validation_status"],
        "execution_plan_validation_status": execution_plan_validation["plan_validation_status"],
        "step_runs": step_runs,
        "blocked_constraints": blocked_constraints,
        "out_of_scope_constraints": out_of_scope_constraints,
        "summary": {
            "total_step_count": len(step_runs),
            "completed_step_count": sum(1 for step in step_runs if step["status"] == "completed"),
            "failed_step_count": sum(1 for step in step_runs if step["status"] == "failed"),
            "pending_step_count": sum(1 for step in step_runs if step["status"] == "pending"),
            "blocked_constraint_count": len(blocked_constraints),
            "out_of_scope_constraint_count": len(out_of_scope_constraints),
        },
    }

    plan_run_path = agent_dir / "plan_run_v1.json"
    write_json_atomic(plan_run_path, plan_run)

    return {
        "job_id": job_id,
        "plan_run_path": str(plan_run_path),
        "plan_run_id": plan_run["plan_run_id"],
        "run_status": plan_run["run_status"],
        "total_step_count": plan_run["summary"]["total_step_count"],
        "completed_step_count": plan_run["summary"]["completed_step_count"],
        "failed_step_count": plan_run["summary"]["failed_step_count"],
        "pending_step_count": plan_run["summary"]["pending_step_count"],
        "blocked_constraint_count": plan_run["summary"]["blocked_constraint_count"],
        "out_of_scope_constraint_count": plan_run["summary"]["out_of_scope_constraint_count"],
        "agent_plan_validation_status": plan_run["agent_plan_validation_status"],
        "execution_plan_validation_status": plan_run["execution_plan_validation_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_plan_run(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Run an artifact-aware Agent executor for one renewable-site job.

Executor v1 does not blindly rerun GIS tools. It reconciles the persisted
Agent Plan with verified execution artifacts and produces an auditable executor
run record. Missing artifacts are treated as failures, not ignored.
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


def resolve_artifact_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def run_agent_executor(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    agent_plan_path = agent_dir / "agent_plan_v1.json"
    execution_plan_path = agent_dir / "execution_plan_v1.json"

    agent_plan_validation = validate_agent_plan(job_id)
    execution_plan_validation = validate_execution_plan(job_id)

    agent_plan = read_json(agent_plan_path)
    execution_plan = read_json(execution_plan_path)

    execution_steps_by_tool_id = {
        step["registered_tool_id"]: step
        for step in execution_plan.get("completed_steps", [])
        if "registered_tool_id" in step
    }

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    executor_steps: list[dict[str, Any]] = []

    for planned_step in agent_plan["planned_steps"]:
        tool_id = planned_step["registered_tool_id"]
        execution_step = execution_steps_by_tool_id.get(tool_id)

        artifact_checks: list[dict[str, Any]] = []

        if execution_step is None:
            status = "pending"
            status_reason = "No completed execution record exists for this planned tool."
        else:
            outputs = execution_step.get("outputs", [])
            missing_artifacts: list[dict[str, Any]] = []

            for output in outputs:
                path_text = output.get("path")
                role = output.get("role")
                if not path_text:
                    missing_artifacts.append(
                        {
                            "role": role,
                            "path": None,
                            "exists": False,
                            "reason": "Output path is missing.",
                        }
                    )
                    continue

                artifact_path = resolve_artifact_path(path_text)
                exists = artifact_path.is_file()
                check = {
                    "role": role,
                    "path": str(artifact_path),
                    "exists": exists,
                }
                artifact_checks.append(check)

                if not exists:
                    missing_artifacts.append(check)

            if missing_artifacts:
                status = "failed"
                status_reason = "One or more expected artifacts are missing."
            else:
                status = "completed"
                status_reason = "Execution record and expected artifacts are present."

        executor_steps.append(
            {
                "sequence_index": planned_step["sequence_index"],
                "registered_tool_id": tool_id,
                "registered_tool_module": planned_step["registered_tool_module"],
                "status": status,
                "status_reason": status_reason,
                "deterministic": planned_step["deterministic"],
                "execution_policy": planned_step["execution_policy"],
                "artifact_checks": artifact_checks,
                "checked_at_utc": now,
            }
        )

    status_counts = {
        "completed": sum(1 for step in executor_steps if step["status"] == "completed"),
        "failed": sum(1 for step in executor_steps if step["status"] == "failed"),
        "pending": sum(1 for step in executor_steps if step["status"] == "pending"),
    }

    blocked_constraints = agent_plan.get("blocked_constraints", [])
    out_of_scope_constraints = agent_plan.get("out_of_scope_constraints", [])

    if status_counts["failed"] > 0:
        executor_status = "failed"
        executor_status_zh = "执行器检查失败"
    elif status_counts["pending"] > 0:
        executor_status = "pending"
        executor_status_zh = "仍有步骤等待执行"
    elif blocked_constraints:
        executor_status = "completed_with_blocked_constraints"
        executor_status_zh = "当前范围步骤已完成，但仍有阻塞约束"
    else:
        executor_status = "completed"
        executor_status_zh = "当前产品范围内执行完成"

    executor_run = {
        "job_id": job_id,
        "executor_run_id": f"executor-run-{job_id}-v1",
        "executor_version": 1,
        "created_at_utc": now,
        "executor_mode": "artifact_aware_reconciliation",
        "executor_mode_zh": "产物感知式执行核对",
        "executor_status": executor_status,
        "executor_status_zh": executor_status_zh,
        "agent_plan_validation_status": agent_plan_validation["agent_plan_validation_status"],
        "execution_plan_validation_status": execution_plan_validation["plan_validation_status"],
        "steps": executor_steps,
        "blocked_constraints": blocked_constraints,
        "out_of_scope_constraints": out_of_scope_constraints,
        "summary": {
            "total_step_count": len(executor_steps),
            "completed_step_count": status_counts["completed"],
            "failed_step_count": status_counts["failed"],
            "pending_step_count": status_counts["pending"],
            "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
            "out_of_scope_constraint_count": len(out_of_scope_constraints),
        },
        "safety_boundary": (
            "Executor v1 reconciles existing verified artifacts. It does not invent "
            "GIS results and does not mark missing artifacts as completed."
        ),
        "safety_boundary_zh": (
            "执行器 v1 只核对已有且已验证的产物，不编造 GIS 结果，也不会把缺失产物标记为完成。"
        ),
    }

    executor_run_path = agent_dir / "executor_run_v1.json"
    write_json_atomic(executor_run_path, executor_run)

    return {
        "job_id": job_id,
        "executor_run_path": str(executor_run_path),
        "executor_status": executor_status,
        "executor_status_zh": executor_status_zh,
        "total_step_count": len(executor_steps),
        "completed_step_count": status_counts["completed"],
        "failed_step_count": status_counts["failed"],
        "pending_step_count": status_counts["pending"],
        "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "agent_plan_validation_status": executor_run["agent_plan_validation_status"],
        "execution_plan_validation_status": executor_run["execution_plan_validation_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = run_agent_executor(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Simulate one executor failure and create a simulated resume policy.

This script does not modify real artifacts or the real executor_run_v1.json.
It writes simulation-only files under runtime/jobs/<job_id>/agent/simulations/.
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_control_plane import (
    validate_agent_control_plane,
)


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"
DEFAULT_FAIL_TOOL_ID = "prepare_terrain_dem"


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


def get_step_tool_id(step: dict[str, Any]) -> str | None:
    return (
        step.get("registered_tool_id")
        or step.get("tool_id")
        or step.get("step_id")
        or step.get("id")
    )


def count_statuses(steps: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "completed": sum(1 for step in steps if step.get("status") == "completed"),
        "failed": sum(1 for step in steps if step.get("status") == "failed"),
        "pending": sum(1 for step in steps if step.get("status") == "pending"),
    }


def simulate_failure(job_id: str, fail_tool_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"
    simulation_dir = agent_dir / "simulations"

    real_executor_path = agent_dir / "executor_run_v1.json"
    real_executor = read_json(real_executor_path)

    # Safety check: the real control plane must be healthy before simulation.
    real_control_plane = validate_agent_control_plane(job_id)

    simulated_executor = copy.deepcopy(real_executor)
    steps = simulated_executor.get("steps", [])
    if not steps:
        raise RuntimeError("executor_run_v1.json has no steps.")

    target_step = None
    for step in steps:
        if get_step_tool_id(step) == fail_tool_id:
            target_step = step
            break

    if target_step is None:
        raise RuntimeError(f"Cannot find target step for failure simulation: {fail_tool_id}")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    original_status = target_step.get("status")
    target_step["status"] = "failed"
    target_step["status_reason"] = (
        "Simulated failure: required artifact is treated as missing for resume-policy testing."
    )
    target_step["status_reason_zh"] = "失败模拟：为了测试断点恢复策略，假设该步骤必需产物缺失。"
    target_step["failure_simulation"] = {
        "enabled": True,
        "created_at_utc": now,
        "original_status": original_status,
        "simulated_failure_kind": "missing_required_artifact",
        "simulated_failure_kind_zh": "模拟必需产物缺失",
    }

    artifact_checks = target_step.get("artifact_checks")
    if not isinstance(artifact_checks, list):
        artifact_checks = []
        target_step["artifact_checks"] = artifact_checks

    if artifact_checks:
        artifact_checks[0]["original_exists_before_simulation"] = artifact_checks[0].get("exists")
        artifact_checks[0]["exists"] = False
        artifact_checks[0]["simulated_missing"] = True
        artifact_checks[0]["simulation_note"] = (
            "This artifact is not deleted. It is only marked missing in the simulation record."
        )
    else:
        artifact_checks.append(
            {
                "artifact_name": "simulated_required_artifact",
                "path": "__simulation_only_missing_artifact__",
                "exists": False,
                "simulated_missing": True,
                "simulation_note": (
                    "No real file is deleted. This is a synthetic missing-artifact check."
                ),
            }
        )

    status_counts = count_statuses(steps)
    blocked_constraints = simulated_executor.get("blocked_constraints", [])
    out_of_scope_constraints = simulated_executor.get("out_of_scope_constraints", [])

    simulated_executor["executor_status"] = "failed"
    simulated_executor["executor_status_zh"] = "失败模拟：存在失败步骤"
    simulated_executor["simulation"] = {
        "enabled": True,
        "simulation_kind": "single_step_missing_artifact",
        "simulation_kind_zh": "单步骤产物缺失模拟",
        "created_at_utc": now,
        "target_tool_id": fail_tool_id,
        "real_executor_path": str(real_executor_path),
        "real_control_plane_status_before_simulation": real_control_plane[
            "agent_control_plane_validation_status"
        ],
        "safety_note": (
            "This simulation does not modify real artifacts or the real executor_run_v1.json."
        ),
        "safety_note_zh": "本模拟不会修改真实产物，也不会修改真实 executor_run_v1.json。",
    }

    simulated_executor["summary"] = {
        **simulated_executor.get("summary", {}),
        "total_step_count": len(steps),
        "completed_step_count": status_counts["completed"],
        "failed_step_count": status_counts["failed"],
        "pending_step_count": status_counts["pending"],
        "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
    }

    failed_steps = [step for step in steps if step.get("status") == "failed"]
    pending_steps = [step for step in steps if step.get("status") == "pending"]

    if failed_steps:
        policy_status = "resume_after_failure_fix"
        policy_status_zh = "修复失败步骤后恢复执行"
        automatic_resume_allowed = False
        next_resume_step = get_step_tool_id(failed_steps[0])
        resume_strategy = "fix_failed_artifacts_then_resume_from_first_failed_step"
        resume_strategy_zh = "修复失败产物后，从第一个失败步骤继续执行。"
    elif pending_steps:
        policy_status = "resume_from_pending_step"
        policy_status_zh = "从等待步骤恢复执行"
        automatic_resume_allowed = True
        next_resume_step = get_step_tool_id(pending_steps[0])
        resume_strategy = "resume_from_first_pending_step"
        resume_strategy_zh = "从第一个等待步骤继续执行。"
    else:
        policy_status = "no_resume_needed_current_scope_completed"
        policy_status_zh = "当前产品范围已完成，无需恢复"
        automatic_resume_allowed = False
        next_resume_step = None
        resume_strategy = "current_scope_completed_no_resume_needed"
        resume_strategy_zh = "当前产品范围内流程已完成，不需要断点恢复。"

    simulated_resume_policy = {
        "job_id": job_id,
        "resume_policy_id": f"resume-policy-failure-simulation-{job_id}-v1",
        "resume_policy_version": 1,
        "created_at_utc": now,
        "simulation": {
            "enabled": True,
            "source": "executor_run_failure_simulation_v1.json",
            "target_tool_id": fail_tool_id,
        },
        "policy_status": policy_status,
        "policy_status_zh": policy_status_zh,
        "automatic_resume_allowed": automatic_resume_allowed,
        "next_resume_step": next_resume_step,
        "resume_strategy": resume_strategy,
        "resume_strategy_zh": resume_strategy_zh,
        "failed_step_count": len(failed_steps),
        "pending_step_count": len(pending_steps),
        "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "failed_steps": failed_steps,
        "pending_steps": pending_steps,
        "blocked_constraints": blocked_constraints,
        "out_of_scope_constraints": out_of_scope_constraints,
        "safety_boundary": (
            "This is a simulation-only resume policy. It must not replace the real "
            "resume_policy_v1.json."
        ),
        "safety_boundary_zh": (
            "这是仅用于模拟的断点恢复策略，不能替换真实 resume_policy_v1.json。"
        ),
    }

    simulated_executor_path = simulation_dir / "executor_run_failure_simulation_v1.json"
    simulated_policy_path = simulation_dir / "resume_policy_failure_simulation_v1.json"

    write_json_atomic(simulated_executor_path, simulated_executor)
    write_json_atomic(simulated_policy_path, simulated_resume_policy)

    return {
        "job_id": job_id,
        "simulation_status": "created",
        "simulation_status_zh": "失败模拟已创建",
        "target_tool_id": fail_tool_id,
        "simulated_executor_path": str(simulated_executor_path),
        "simulated_resume_policy_path": str(simulated_policy_path),
        "real_executor_path": str(real_executor_path),
        "real_control_plane_status_before_simulation": real_control_plane[
            "agent_control_plane_validation_status"
        ],
        "simulated_executor_status": simulated_executor["executor_status"],
        "simulated_failed_step_count": len(failed_steps),
        "simulated_pending_step_count": len(pending_steps),
        "simulated_blocked_constraint_count": len(blocked_constraints),
        "simulated_out_of_scope_constraint_count": len(out_of_scope_constraints),
        "simulated_resume_policy_status": simulated_resume_policy["policy_status"],
        "simulated_resume_policy_status_zh": simulated_resume_policy["policy_status_zh"],
        "simulated_next_resume_step": simulated_resume_policy["next_resume_step"],
        "simulated_automatic_resume_allowed": simulated_resume_policy[
            "automatic_resume_allowed"
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    parser.add_argument("--fail-tool-id", default=DEFAULT_FAIL_TOOL_ID)
    args = parser.parse_args()

    result = simulate_failure(args.job_id, args.fail_tool_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

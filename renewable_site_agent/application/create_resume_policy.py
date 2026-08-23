"""Create a scope-aware resume and failure policy for an Agent job."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_executor import validate_agent_executor
from renewable_site_agent.application.validate_plan_run import validate_plan_run
from renewable_site_agent.application.validate_scope_decision import validate_scope_decision


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


def create_resume_policy(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    executor_validation = validate_agent_executor(job_id)
    plan_run_validation = validate_plan_run(job_id)
    scope_validation = validate_scope_decision(job_id)

    executor_run = read_json(agent_dir / "executor_run_v1.json")
    scope_decision = read_json(agent_dir / "scope_decision_v1.json")

    failed_steps = [
        step for step in executor_run["steps"] if step["status"] == "failed"
    ]
    pending_steps = [
        step for step in executor_run["steps"] if step["status"] == "pending"
    ]

    blocked_constraints = executor_run.get("blocked_constraints", [])
    out_of_scope_constraints = scope_decision.get("out_of_scope_constraints", [])

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if failed_steps:
        policy_status = "resume_after_failure_fix"
        policy_status_zh = "修复失败步骤后恢复执行"
        automatic_resume_allowed = False
        next_resume_step = failed_steps[0]["registered_tool_id"]
        resume_strategy = "fix_failed_artifacts_then_resume_from_first_failed_step"
        resume_strategy_zh = "修复失败产物后，从第一个失败步骤继续执行。"
    elif pending_steps:
        policy_status = "resume_from_pending_step"
        policy_status_zh = "从等待步骤恢复执行"
        automatic_resume_allowed = True
        next_resume_step = pending_steps[0]["registered_tool_id"]
        resume_strategy = "resume_from_first_pending_step"
        resume_strategy_zh = "从第一个等待步骤继续执行。"
    elif blocked_constraints:
        policy_status = "blocked_requires_manual_resolution"
        policy_status_zh = "存在阻塞约束，需要人工处理"
        automatic_resume_allowed = False
        next_resume_step = None
        resume_strategy = "resolve_blocked_constraints_before_resume"
        resume_strategy_zh = "先处理阻塞约束，再恢复执行。"
    else:
        policy_status = "no_resume_needed_current_scope_completed"
        policy_status_zh = "当前产品范围已完成，无需恢复"
        automatic_resume_allowed = False
        next_resume_step = None
        resume_strategy = "current_scope_completed_no_resume_needed"
        resume_strategy_zh = "当前产品范围内流程已完成，不需要断点恢复。"

    policy = {
        "job_id": job_id,
        "resume_policy_id": f"resume-policy-{job_id}-v1",
        "resume_policy_version": 1,
        "created_at_utc": now,
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
        "future_scope_change_policy": "create_new_plan_version_not_resume_current_run",
        "future_scope_change_policy_zh": "如果未来扩大产品范围，应创建新的计划版本，而不是恢复当前运行。",
        "executor_validation_status": executor_validation[
            "agent_executor_validation_status"
        ],
        "plan_run_validation_status": plan_run_validation[
            "plan_run_validation_status"
        ],
        "scope_decision_validation_status": scope_validation[
            "scope_decision_validation_status"
        ],
        "safety_boundary": (
            "Out-of-scope constraints are transparent product-scope exclusions. "
            "They must not be described as evaluated or satisfied. If they are added "
            "to a future product scope, a new plan version is required."
        ),
        "safety_boundary_zh": (
            "范围外约束是透明记录的产品范围排除项，不能描述为已评估或已满足。"
            "如果未来把它们加入产品范围，必须创建新的计划版本。"
        ),
    }

    policy_path = agent_dir / "resume_policy_v1.json"
    write_json_atomic(policy_path, policy)

    return {
        "job_id": job_id,
        "resume_policy_path": str(policy_path),
        "policy_status": policy_status,
        "policy_status_zh": policy_status_zh,
        "automatic_resume_allowed": automatic_resume_allowed,
        "next_resume_step": next_resume_step,
        "failed_step_count": len(failed_steps),
        "pending_step_count": len(pending_steps),
        "blocked_constraint_count": len(blocked_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "future_scope_change_policy": policy["future_scope_change_policy"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_resume_policy(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

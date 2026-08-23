"""Create a dynamic replanning record for future product-scope changes."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_control_plane import (
    validate_agent_control_plane,
)
from renewable_site_agent.application.validate_scope_decision import validate_scope_decision
from renewable_site_agent.application.validate_resume_policy import validate_resume_policy


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


def create_dynamic_replanning_record(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    control_plane = validate_agent_control_plane(job_id)
    scope_validation = validate_scope_decision(job_id)
    resume_policy = validate_resume_policy(job_id)

    scope_decision = read_json(agent_dir / "scope_decision_v1.json")
    out_of_scope_constraints = scope_decision.get("out_of_scope_constraints", [])

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    record = {
        "job_id": job_id,
        "dynamic_replanning_record_id": f"dynamic-replanning-{job_id}-v1",
        "record_version": 1,
        "created_at_utc": now,
        "current_plan_version": 1,
        "current_scope_status": control_plane["operational_status"],
        "current_scope_status_zh": control_plane["operational_status_zh"],
        "current_result_status": "final_within_current_product_scope",
        "current_result_status_zh": "当前产品范围内的最终结果",
        "current_plan_must_not_be_overwritten": True,
        "current_plan_must_not_be_overwritten_zh": "当前计划版本不能被覆盖。",
        "dynamic_replanning_needed_now": False,
        "dynamic_replanning_needed_now_zh": "当前不需要动态重规划。",
        "future_scope_change_policy": resume_policy["future_scope_change_policy"],
        "future_scope_change_policy_zh": "如果未来扩大产品范围，应创建新的计划版本，而不是恢复当前运行。",
        "next_plan_version_if_scope_expands": 2,
        "replanning_trigger_conditions": [
            {
                "trigger": "add_out_of_scope_constraint_to_product_scope",
                "trigger_zh": "把当前范围外约束加入产品范围",
                "action": "create_new_plan_version",
                "action_zh": "创建新的计划版本",
            },
            {
                "trigger": "add_new_data_source_or_screening_layer",
                "trigger_zh": "新增数据源或筛选图层",
                "action": "create_new_plan_version",
                "action_zh": "创建新的计划版本",
            },
            {
                "trigger": "change_hard_constraint_threshold",
                "trigger_zh": "修改硬性约束阈值",
                "action": "create_new_plan_version",
                "action_zh": "创建新的计划版本",
            },
            {
                "trigger": "change_product_scope_definition",
                "trigger_zh": "修改产品范围定义",
                "action": "create_new_plan_version",
                "action_zh": "创建新的计划版本",
            },
        ],
        "out_of_scope_constraints_available_for_future_replanning": out_of_scope_constraints,
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "blocked_constraint_count": control_plane["blocked_constraint_count"],
        "candidate_count": control_plane["candidate_count"],
        "validation_dependencies": {
            "agent_control_plane": control_plane["agent_control_plane_validation_status"],
            "scope_decision": scope_validation["scope_decision_validation_status"],
            "resume_policy": resume_policy["resume_policy_validation_status"],
        },
        "audit_note": (
            "The current v1 result remains valid as the final result within the current "
            "product scope. Future scope expansion must create a new plan version instead "
            "of mutating or resuming the completed v1 run."
        ),
        "audit_note_zh": (
            "当前 v1 结果仍然是当前产品范围内的最终结果。未来如果扩大产品范围，"
            "必须创建新的计划版本，不能修改或恢复已经完成的 v1 运行。"
        ),
    }

    record_path = agent_dir / "dynamic_replanning_record_v1.json"
    write_json_atomic(record_path, record)

    return {
        "job_id": job_id,
        "dynamic_replanning_record_path": str(record_path),
        "dynamic_replanning_needed_now": record["dynamic_replanning_needed_now"],
        "current_plan_version": record["current_plan_version"],
        "next_plan_version_if_scope_expands": record[
            "next_plan_version_if_scope_expands"
        ],
        "current_result_status": record["current_result_status"],
        "future_scope_change_policy": record["future_scope_change_policy"],
        "out_of_scope_constraint_count": record["out_of_scope_constraint_count"],
        "blocked_constraint_count": record["blocked_constraint_count"],
        "candidate_count": record["candidate_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_dynamic_replanning_record(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

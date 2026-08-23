"""Create a blocked-decision record from PlanRun state.

This record prevents available-data preliminary results from being treated as
final recommendations when hard constraints remain unevaluated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_plan_run import validate_plan_run


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


def create_blocked_decision(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"
    plan_run_path = agent_dir / "plan_run_v1.json"

    plan_run_validation = validate_plan_run(job_id)
    plan_run = read_json(plan_run_path)

    blocked_constraints = plan_run.get("blocked_constraints", [])
    has_blocked_constraints = len(blocked_constraints) > 0

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    required_actions = []
    for item in blocked_constraints:
        constraint = item.get("constraint")
        if constraint == "max_distance_to_substation_km":
            required_actions.append(
                {
                    "constraint": constraint,
                    "required_action": "Provide a reliable substation geospatial layer, then run a distance-screening plan version.",
                    "required_action_zh": "补充可靠的变电站空间图层，然后运行距离筛选计划版本。",
                }
            )
        elif constraint == "exclude_prime_farmland":
            required_actions.append(
                {
                    "constraint": constraint,
                    "required_action": "Provide a reliable prime farmland or land-use restriction layer, then run a land-use screening plan version.",
                    "required_action_zh": "补充可靠的基本农田或土地利用限制图层，然后运行土地利用筛选计划版本。",
                }
            )
        else:
            required_actions.append(
                {
                    "constraint": str(constraint),
                    "required_action": "Resolve the blocked constraint with a verified data source or human review.",
                    "required_action_zh": "使用已验证数据源或人工复核解决该阻塞约束。",
                }
            )

    decision_status = (
        "human_or_data_action_required"
        if has_blocked_constraints
        else "ready_for_human_review"
    )

    decision = {
        "job_id": job_id,
        "decision_id": f"blocked-decision-{job_id}-v1",
        "decision_version": 1,
        "created_at_utc": now,
        "decision_status": decision_status,
        "decision_status_zh": (
            "需要人工或数据源补充"
            if has_blocked_constraints
            else "可进入人工复核"
        ),
        "final_recommendation_allowed": not has_blocked_constraints,
        "preliminary_result_allowed": True,
        "preliminary_result_label": "preliminary_not_final_recommendation",
        "preliminary_result_label_zh": "初步结果，不是最终推荐场址",
        "blocked_constraint_count": len(blocked_constraints),
        "blocked_constraints": blocked_constraints,
        "required_actions": required_actions,
        "plan_run_validation_status": plan_run_validation["plan_run_validation_status"],
        "run_status": plan_run_validation["run_status"],
        "resume_policy": {
            "resume_allowed": True,
            "resume_strategy": "create_new_plan_version_after_required_data_sources_are_added",
            "resume_strategy_zh": "补充所需数据源后创建新的计划版本继续执行。",
        },
        "safety_boundary": (
            "The current available-data result may be shown as a preliminary screening result, "
            "but it must not be presented as a final recommended site list."
        ),
        "safety_boundary_zh": (
            "当前可用数据结果可以作为初步筛选结果展示，但不能作为最终推荐场址列表。"
        ),
    }

    decision_path = agent_dir / "blocked_decision_v1.json"
    write_json_atomic(decision_path, decision)

    return {
        "job_id": job_id,
        "decision_path": str(decision_path),
        "decision_status": decision["decision_status"],
        "decision_status_zh": decision["decision_status_zh"],
        "final_recommendation_allowed": decision["final_recommendation_allowed"],
        "preliminary_result_allowed": decision["preliminary_result_allowed"],
        "blocked_constraint_count": decision["blocked_constraint_count"],
        "required_action_count": len(required_actions),
        "plan_run_validation_status": decision["plan_run_validation_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_blocked_decision(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

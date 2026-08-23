"""Create a single scope-aware agent-state summary for API, UI, audit, and report use."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_available_data_job import validate_job
from renewable_site_agent.application.validate_agent_plan import validate_agent_plan
from renewable_site_agent.application.validate_plan_run import validate_plan_run
from renewable_site_agent.application.validate_agent_executor import validate_agent_executor
from renewable_site_agent.application.validate_scope_decision import validate_scope_decision
from renewable_site_agent.application.validate_resume_policy import validate_resume_policy
from renewable_site_agent.application.validate_dynamic_replanning_record import validate_dynamic_replanning_record


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def create_agent_state_summary(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    available_data_validation = validate_job(job_id)
    agent_plan_validation = validate_agent_plan(job_id)
    plan_run_validation = validate_plan_run(job_id)
    agent_executor_validation = validate_agent_executor(job_id)
    scope_decision_validation = validate_scope_decision(job_id)
    resume_policy_validation = validate_resume_policy(job_id)
    dynamic_replanning_validation = validate_dynamic_replanning_record(job_id)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    final_recommendation_allowed = scope_decision_validation[
        "current_version_recommendation_allowed"
    ]
    out_of_scope_constraint_count = scope_decision_validation[
        "out_of_scope_constraint_count"
    ]
    blocked_constraint_count = scope_decision_validation["blocked_constraint_count"]

    operational_status = "current_scope_completed"
    operational_status_zh = "当前产品范围已完成"

    summary = {
        "job_id": job_id,
        "summary_id": f"agent-state-summary-{job_id}-v1",
        "summary_version": 1,
        "created_at_utc": now,
        "operational_status": operational_status,
        "operational_status_zh": operational_status_zh,
        "final_recommendation_allowed": final_recommendation_allowed,
        "final_recommendation_label": "final_within_current_product_scope",
        "final_recommendation_label_zh": "当前产品范围内的最终推荐",
        "preliminary_result_allowed": True,
        "candidate_count": available_data_validation["final_candidate_count"],
        "candidate_area_km2": available_data_validation["total_area_km2"],
        "screening_status": "final_within_current_product_scope",
        "screening_status_zh": "当前产品范围内的最终结果",
        "validation_statuses": {
            "available_data_job": available_data_validation["validation_status"],
            "agent_plan": agent_plan_validation["agent_plan_validation_status"],
            "plan_run": plan_run_validation["plan_run_validation_status"],
            "agent_executor": agent_executor_validation["agent_executor_validation_status"],
            "scope_decision": scope_decision_validation["scope_decision_validation_status"],
            "resume_policy": resume_policy_validation["resume_policy_validation_status"],
            "dynamic_replanning_record": dynamic_replanning_validation[
                "dynamic_replanning_record_validation_status"
            ],
        },
        "plan_status": agent_plan_validation["plan_status"],
        "run_status": plan_run_validation["run_status"],
        "executor_status": agent_executor_validation["executor_status"],
        "executor_status_zh": agent_executor_validation["executor_status_zh"],
        "product_scope_status": scope_decision_validation["product_scope_status"],
        "product_scope_status_zh": scope_decision_validation["product_scope_status_zh"],
        "resume_policy_status": resume_policy_validation["policy_status"],
        "resume_policy_status_zh": resume_policy_validation["policy_status_zh"],
        "automatic_resume_allowed": resume_policy_validation["automatic_resume_allowed"],
        "future_scope_change_policy": resume_policy_validation["future_scope_change_policy"],
        "dynamic_replanning_needed_now": dynamic_replanning_validation[
            "dynamic_replanning_needed_now"
        ],
        "current_plan_version": dynamic_replanning_validation["current_plan_version"],
        "next_plan_version_if_scope_expands": dynamic_replanning_validation[
            "next_plan_version_if_scope_expands"
        ],
        "replanning_trigger_count": dynamic_replanning_validation[
            "replanning_trigger_count"
        ],
        "planned_step_count": agent_plan_validation["planned_step_count"],
        "completed_step_count": plan_run_validation["completed_step_count"],
        "failed_step_count": plan_run_validation["failed_step_count"],
        "pending_step_count": plan_run_validation["pending_step_count"],
        "blocked_constraint_count": blocked_constraint_count,
        "out_of_scope_constraint_count": out_of_scope_constraint_count,
        "artifact_paths": {
            "agent_plan": str(agent_dir / "agent_plan_v1.json"),
            "execution_plan": str(agent_dir / "execution_plan_v1.json"),
            "execution_plan_verification": str(
                agent_dir / "execution_plan_v1.verification.json"
            ),
            "plan_run": str(agent_dir / "plan_run_v1.json"),
            "executor_run": str(agent_dir / "executor_run_v1.json"),
            "scope_decision": str(agent_dir / "scope_decision_v1.json"),
            "resume_policy": str(agent_dir / "resume_policy_v1.json"),
            "dynamic_replanning_record": str(
                agent_dir / "dynamic_replanning_record_v1.json"
            ),
            "final_candidates": str(
                job_dir / "final" / "available_data_screened_candidate_sites.geojson"
            ),
            "map_preview": str(job_dir / "map_preview" / "candidate_preview.geojson"),
        },
        "scope_boundary": (
            "The recommendation is final within the current product scope only. "
            "Out-of-scope constraints are explicitly recorded and must not be described "
            "as evaluated or satisfied."
        ),
        "scope_boundary_zh": (
            "推荐结果仅在当前产品范围内是最终结果。范围外约束已明确记录，不能描述为已评估或已满足。"
        ),
    }

    summary_path = agent_dir / "agent_state_summary_v1.json"
    write_json_atomic(summary_path, summary)

    return {
        "job_id": job_id,
        "summary_path": str(summary_path),
        "operational_status": summary["operational_status"],
        "operational_status_zh": summary["operational_status_zh"],
        "final_recommendation_allowed": summary["final_recommendation_allowed"],
        "final_recommendation_label": summary["final_recommendation_label"],
        "candidate_count": summary["candidate_count"],
        "blocked_constraint_count": summary["blocked_constraint_count"],
        "out_of_scope_constraint_count": summary["out_of_scope_constraint_count"],
        "executor_status": summary["executor_status"],
        "executor_status_zh": summary["executor_status_zh"],
        "resume_policy_status": summary["resume_policy_status"],
        "resume_policy_status_zh": summary["resume_policy_status_zh"],
        "automatic_resume_allowed": summary["automatic_resume_allowed"],
        "dynamic_replanning_needed_now": summary["dynamic_replanning_needed_now"],
        "current_plan_version": summary["current_plan_version"],
        "next_plan_version_if_scope_expands": summary[
            "next_plan_version_if_scope_expands"
        ],
        "all_validation_statuses": summary["validation_statuses"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_agent_state_summary(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

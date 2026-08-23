"""Create a product-scope decision for the current renewable-site Agent version.

This file records which constraints are part of the current product scope and
which constraints are deliberately out of scope. Out-of-scope constraints must
not be treated as evaluated or satisfied.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_available_data_job import validate_job


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def create_scope_decision(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    agent_dir = job_dir / "agent"

    available_data_validation = validate_job(job_id)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    in_scope_constraints = [
        {
            "constraint": "administrative_boundary",
            "status": "evaluated",
            "status_zh": "已评估",
            "decision_role": "hard_constraint",
            "decision_role_zh": "硬性约束",
        },
        {
            "constraint": "exclude_water",
            "status": "evaluated",
            "status_zh": "已评估",
            "decision_role": "hard_constraint",
            "decision_role_zh": "硬性约束",
        },
        {
            "constraint": "max_slope_deg",
            "status": "evaluated",
            "status_zh": "已评估",
            "decision_role": "hard_constraint",
            "decision_role_zh": "硬性约束",
        },
        {
            "constraint": "exclude_protected_areas",
            "status": "evaluated",
            "status_zh": "已评估",
            "decision_role": "hard_constraint",
            "decision_role_zh": "硬性约束",
        },
    ]

    out_of_scope_constraints = [
        {
            "constraint": "max_distance_to_substation_km",
            "status": "out_of_scope_by_product_decision",
            "status_zh": "按产品决策不纳入当前版本范围",
            "reason": "Substation-distance screening is not part of the current product scope.",
            "reason_zh": "变电站距离筛选不纳入当前版本产品范围。",
            "must_not_claim_evaluated": True,
        },
        {
            "constraint": "exclude_prime_farmland",
            "status": "out_of_scope_by_product_decision",
            "status_zh": "按产品决策不纳入当前版本范围",
            "reason": "Prime farmland or land-use restriction screening is not part of the current product scope.",
            "reason_zh": "基本农田或土地利用限制筛选不纳入当前版本产品范围。",
            "must_not_claim_evaluated": True,
        },
    ]

    scope_decision = {
        "job_id": job_id,
        "scope_decision_id": f"scope-decision-{job_id}-v1",
        "scope_version": 1,
        "created_at_utc": now,
        "product_scope_status": "current_version_scope_finalized",
        "product_scope_status_zh": "当前版本产品范围已确定",
        "in_scope_constraints": in_scope_constraints,
        "out_of_scope_constraints": out_of_scope_constraints,
        "in_scope_constraint_count": len(in_scope_constraints),
        "out_of_scope_constraint_count": len(out_of_scope_constraints),
        "blocked_constraint_count": 0,
        "current_version_recommendation_allowed": True,
        "current_version_recommendation_label": "final_within_current_product_scope",
        "current_version_recommendation_label_zh": "当前产品范围内的最终推荐",
        "candidate_count": available_data_validation["final_candidate_count"],
        "candidate_area_km2": available_data_validation["total_area_km2"],
        "available_data_validation_status": available_data_validation[
            "validation_status"
        ],
        "scope_boundary": (
            "Recommendations are final only within the current product scope. "
            "Out-of-scope constraints are explicitly not evaluated and must not be "
            "described as satisfied."
        ),
        "scope_boundary_zh": (
            "推荐结果仅在当前产品范围内是最终结果。范围外约束明确未评估，不能描述为已满足。"
        ),
    }

    scope_path = agent_dir / "scope_decision_v1.json"
    write_json_atomic(scope_path, scope_decision)

    return {
        "job_id": job_id,
        "scope_decision_path": str(scope_path),
        "product_scope_status": scope_decision["product_scope_status"],
        "product_scope_status_zh": scope_decision["product_scope_status_zh"],
        "current_version_recommendation_allowed": scope_decision[
            "current_version_recommendation_allowed"
        ],
        "current_version_recommendation_label": scope_decision[
            "current_version_recommendation_label"
        ],
        "in_scope_constraint_count": scope_decision["in_scope_constraint_count"],
        "out_of_scope_constraint_count": scope_decision[
            "out_of_scope_constraint_count"
        ],
        "blocked_constraint_count": scope_decision["blocked_constraint_count"],
        "candidate_count": scope_decision["candidate_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_scope_decision(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

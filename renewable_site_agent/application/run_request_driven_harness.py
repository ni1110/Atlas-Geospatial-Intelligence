"""Request-driven Agent harness.

This harness runs multiple natural-language renewable-site requests through:
request parsing -> scope resolving -> dynamic plan -> dynamic job -> execution.

It is used to verify that the Agent is not only a fixed-job demo.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.agent.request_scope import parse_and_resolve
from renewable_site_agent.agent.dynamic_plan_builder import build_dynamic_plan
from renewable_site_agent.application.create_dynamic_request_job import (
    create_dynamic_request_job,
)
from renewable_site_agent.application.run_dynamic_request_job import (
    run_dynamic_request_job,
)
from renewable_site_agent.application.create_job import RUNTIME_ROOT


HARNESS_CASES = [
    {
        "case_id": "case_001_renewable_water_only",
        "request_text": "我想在北京市找新能源场站候选区域，要求避开水体",
        "expected_project_type": "renewable_site",
        "expected_scope_kind": "exclude_water",
        "expected_status_prefix": "completed",
    },
    {
        "case_id": "case_002_solar_water_only",
        "request_text": "我想在北京市找光伏场站候选区域，只要求避开水体",
        "expected_project_type": "solar",
        "expected_scope_kind": "exclude_water",
        "expected_status_prefix": "completed",
    },
    {
        "case_id": "case_003_wind_protected_with_wind_resource",
        "request_text": "我想在北京市找风电场站候选区域，要求避开保护区，并考虑风资源",
        "expected_project_type": "wind",
        "expected_scope_kind": "exclude_protected_areas",
        "expected_status_prefix": "completed_with_out_of_scope_constraints",
    },
    {
        "case_id": "case_004_storage_water_slope20_grid",
        "request_text": "我想在北京市找储能电站候选区域，要求避开水体，坡度小于20度，并考虑电网接入成本",
        "expected_project_type": "storage",
        "expected_scope_kind": "exclude_water+max_slope_deg",
        "expected_status_prefix": "completed_with_out_of_scope_constraints",
    },
    {
        "case_id": "case_005_renewable_water_slope10",
        "request_text": "我想在北京市找新能源场站候选区域，要求避开水体，坡度小于10度",
        "expected_project_type": "renewable_site",
        "expected_scope_kind": "exclude_water+max_slope_deg",
        "expected_status_prefix": "completed",
    },
    {
        "case_id": "case_006_hybrid_water_protected_slope15",
        "request_text": "我想在北京市找风光储综合能源站候选区域，要求避开水体，避开保护区，坡度小于15度",
        "expected_project_type": "hybrid",
        "expected_scope_kind": "exclude_water+max_slope_deg+exclude_protected_areas",
        "expected_status_prefix": "completed",
    },
]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_one_case(case: dict[str, Any]) -> dict[str, Any]:
    request_text = case["request_text"]

    scope = parse_and_resolve(request_text)
    plan = build_dynamic_plan(scope)

    request_id = scope["request_id"]

    scope_path = RUNTIME_ROOT / "request_scopes" / f"{request_id}_scope.json"
    plan_path = RUNTIME_ROOT / "request_plans" / f"{request_id}_dynamic_agent_plan.json"

    write_json(scope_path, scope)
    write_json(plan_path, plan)

    job_result = create_dynamic_request_job(plan_path)
    execution_result = run_dynamic_request_job(job_result["job_id"])

    expected_project_type = case.get("expected_project_type")
    expected_scope_kind = case.get("expected_scope_kind")
    expected_status_prefix = case.get("expected_status_prefix")

    checks = {
        "project_type_match": scope["parsed_request"].get("project_type")
        == expected_project_type,
        "scope_kind_match": scope.get("scope_kind") == expected_scope_kind,
        "execution_status_match": execution_result["dynamic_execution_status"].startswith(
            expected_status_prefix
        ),
        "no_failure_reason": execution_result.get("failure_reason") is None,
        "candidate_count_available": execution_result.get("candidate_count") is not None,
    }

    case_passed = all(checks.values())

    return {
        "case_id": case["case_id"],
        "request_text": request_text,
        "case_status": "passed" if case_passed else "failed",
        "checks": checks,
        "request_id": request_id,
        "job_id": job_result["job_id"],
        "project_type": scope["parsed_request"].get("project_type"),
        "project_type_zh": scope["parsed_request"].get("project_type_zh"),
        "scope_kind": scope.get("scope_kind"),
        "request_status": scope.get("request_status"),
        "plan_status": plan.get("plan_status"),
        "dynamic_execution_status": execution_result["dynamic_execution_status"],
        "candidate_count": execution_result.get("candidate_count"),
        "failure_reason": execution_result.get("failure_reason"),
        "scope_path": str(scope_path),
        "plan_path": str(plan_path),
        "execution_path": execution_result.get("execution_path"),
        "final_candidate_path": execution_result.get("final_candidate_path"),
    }


def run_harness() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    case_results = []
    for case in HARNESS_CASES:
        try:
            result = run_one_case(case)
        except Exception as exc:
            result = {
                "case_id": case["case_id"],
                "request_text": case["request_text"],
                "case_status": "failed",
                "exception": f"{type(exc).__name__}: {exc}",
            }
        case_results.append(result)

    passed_count = sum(1 for item in case_results if item["case_status"] == "passed")
    failed_count = len(case_results) - passed_count

    harness_status = "passed" if failed_count == 0 else "failed"

    report = {
        "harness_id": "request_driven_agent_harness_v1",
        "created_at_utc": now,
        "harness_status": harness_status,
        "total_case_count": len(case_results),
        "passed_case_count": passed_count,
        "failed_case_count": failed_count,
        "supported_capabilities": [
            "general renewable-site request parsing",
            "solar / wind / storage / hybrid project-type parsing",
            "water exclusion execution",
            "protected-area exclusion execution",
            "dynamic arbitrary numeric slope-threshold execution",
            "out-of-scope constraint recording",
            "request-specific dynamic job creation",
        ],
        "case_results": case_results,
    }

    output_path = RUNTIME_ROOT / "harness" / "request_driven_agent_harness_v1.json"
    write_json(output_path, report)

    return {
        "harness_status": harness_status,
        "total_case_count": len(case_results),
        "passed_case_count": passed_count,
        "failed_case_count": failed_count,
        "harness_report_path": str(output_path),
        "case_summaries": [
            {
                "case_id": item.get("case_id"),
                "case_status": item.get("case_status"),
                "project_type": item.get("project_type"),
                "scope_kind": item.get("scope_kind"),
                "dynamic_execution_status": item.get("dynamic_execution_status"),
                "candidate_count": item.get("candidate_count"),
                "failure_reason": item.get("failure_reason"),
                "exception": item.get("exception"),
            }
            for item in case_results
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = run_harness()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

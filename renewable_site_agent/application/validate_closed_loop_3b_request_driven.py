"""Final validation for Closed Loop 3B: request-driven Agent layer."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


class ClosedLoop3BValidationError(RuntimeError):
    """Raised when Closed Loop 3B validation fails."""


EXPECTED_CASE_IDS = {
    "case_001_renewable_water_only",
    "case_002_solar_water_only",
    "case_003_wind_protected_with_wind_resource",
    "case_004_storage_water_slope20_grid",
    "case_005_renewable_water_slope10",
    "case_006_hybrid_water_protected_slope15",
}

REQUIRED_CAPABILITIES = {
    "general renewable-site request parsing",
    "solar / wind / storage / hybrid project-type parsing",
    "water exclusion execution",
    "protected-area exclusion execution",
    "dynamic arbitrary numeric slope-threshold execution",
    "out-of-scope constraint recording",
    "request-specific dynamic job creation",
}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ClosedLoop3BValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_closed_loop_3b_request_driven() -> dict[str, Any]:
    harness_path = RUNTIME_ROOT / "harness" / "request_driven_agent_harness_v1.json"
    harness = read_json(harness_path)

    if harness.get("harness_status") != "passed":
        raise ClosedLoop3BValidationError(
            f"Harness is not passed: {harness.get('harness_status')}"
        )

    case_results = harness.get("case_results", [])
    if len(case_results) < 6:
        raise ClosedLoop3BValidationError(
            f"Expected at least 6 harness cases, got {len(case_results)}"
        )

    case_ids = {case.get("case_id") for case in case_results}
    missing_case_ids = sorted(EXPECTED_CASE_IDS - case_ids)
    if missing_case_ids:
        raise ClosedLoop3BValidationError(
            f"Missing required harness cases: {missing_case_ids}"
        )

    failed_cases = [
        case for case in case_results if case.get("case_status") != "passed"
    ]
    if failed_cases:
        raise ClosedLoop3BValidationError(
            f"Failed harness cases: {[case.get('case_id') for case in failed_cases]}"
        )

    capabilities = set(harness.get("supported_capabilities", []))
    missing_capabilities = sorted(REQUIRED_CAPABILITIES - capabilities)
    if missing_capabilities:
        raise ClosedLoop3BValidationError(
            f"Missing required capabilities: {missing_capabilities}"
        )

    job_ids = [case.get("job_id") for case in case_results if case.get("job_id")]
    unique_job_ids = set(job_ids)
    if len(job_ids) != len(unique_job_ids):
        raise ClosedLoop3BValidationError("Duplicate job_id found in harness cases.")

    request_ids = [case.get("request_id") for case in case_results if case.get("request_id")]
    if len(request_ids) != len(set(request_ids)):
        raise ClosedLoop3BValidationError("Duplicate request_id found in harness cases.")

    if any(str(job_id).startswith("ce5f4714") for job_id in job_ids):
        raise ClosedLoop3BValidationError(
            "Harness reused the old fixed baseline job id as a request job."
        )

    candidate_counts = [
        case.get("candidate_count")
        for case in case_results
        if isinstance(case.get("candidate_count"), int)
    ]
    if len(candidate_counts) != len(case_results):
        raise ClosedLoop3BValidationError(
            "Every harness case must produce an integer candidate_count."
        )

    project_types = sorted({case.get("project_type") for case in case_results})
    scope_kinds = sorted({case.get("scope_kind") for case in case_results})
    execution_statuses = sorted(
        {case.get("dynamic_execution_status") for case in case_results}
    )

    out_of_scope_cases = [
        case
        for case in case_results
        if case.get("dynamic_execution_status")
        == "completed_with_out_of_scope_constraints"
    ]

    fully_completed_cases = [
        case
        for case in case_results
        if case.get("dynamic_execution_status") == "completed"
    ]

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    validation = {
        "closed_loop_3b_request_driven_validation_status": "passed",
        "closed_loop_3b_status_zh": "闭环 3B 请求驱动 Agent 层验收通过",
        "created_at_utc": now,
        "harness_report_path": str(harness_path),
        "harness_status": harness.get("harness_status"),
        "total_case_count": harness.get("total_case_count"),
        "passed_case_count": harness.get("passed_case_count"),
        "failed_case_count": harness.get("failed_case_count"),
        "project_types_covered": project_types,
        "scope_kinds_covered": scope_kinds,
        "execution_statuses_observed": execution_statuses,
        "fully_completed_case_count": len(fully_completed_cases),
        "completed_with_out_of_scope_case_count": len(out_of_scope_cases),
        "candidate_count_min": min(candidate_counts),
        "candidate_count_max": max(candidate_counts),
        "request_driven_job_count": len(unique_job_ids),
        "unique_request_count": len(set(request_ids)),
        "uses_old_fixed_job_as_request_job": False,
        "supported_execution_capabilities": [
            "water_exclusion",
            "protected_area_exclusion",
            "dynamic_slope_threshold",
        ],
        "supported_project_types": project_types,
        "known_out_of_scope_constraints": [
            "wind_resource",
            "grid_connection_cost",
            "solar_irradiance",
            "prime_farmland",
            "substation_distance",
        ],
        "assessment_zh": (
            "当前系统已从固定 job 控制平面升级为请求驱动 Agent："
            "可解析多类新能源场站请求，动态生成 scope、plan 和 job，"
            "并真实执行水体排除、保护区排除和任意数值坡度阈值筛选。"
            "风资源、电网接入成本、光照资源、基本农田和变电站距离仍作为当前版本范围外约束记录。"
        ),
        "next_stage": "closed_loop_3c_engineering_enhancement",
        "next_stage_zh": "下一步进入闭环 3C：上下文工程、技能包、循环工程和工具协议强化",
    }

    output_path = (
        RUNTIME_ROOT
        / "request_driven_agent"
        / "closed_loop_3b_request_driven_validation_v1.json"
    )
    write_json(output_path, validation)

    return {
        "closed_loop_3b_request_driven_validation_status": validation[
            "closed_loop_3b_request_driven_validation_status"
        ],
        "closed_loop_3b_status_zh": validation["closed_loop_3b_status_zh"],
        "harness_status": validation["harness_status"],
        "total_case_count": validation["total_case_count"],
        "passed_case_count": validation["passed_case_count"],
        "failed_case_count": validation["failed_case_count"],
        "project_types_covered": validation["project_types_covered"],
        "scope_kinds_covered": validation["scope_kinds_covered"],
        "fully_completed_case_count": validation["fully_completed_case_count"],
        "completed_with_out_of_scope_case_count": validation[
            "completed_with_out_of_scope_case_count"
        ],
        "candidate_count_min": validation["candidate_count_min"],
        "candidate_count_max": validation["candidate_count_max"],
        "request_driven_job_count": validation["request_driven_job_count"],
        "validation_report_path": str(output_path),
        "next_stage": validation["next_stage"],
        "next_stage_zh": validation["next_stage_zh"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = validate_closed_loop_3b_request_driven()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

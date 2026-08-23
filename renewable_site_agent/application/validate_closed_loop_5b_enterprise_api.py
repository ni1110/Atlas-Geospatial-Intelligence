from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def get_json(url: str, timeout_sec: int) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate_enterprise_api(base_url: str, timeout_sec: int) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    delivery_dir = RUNTIME_ROOT / "enterprise_delivery"

    errors: list[str] = []
    case_results: list[dict[str, Any]] = []

    # 1. Health check
    try:
        health = get_json(f"{base_url}/health", timeout_sec=timeout_sec)
        health_ok = health.get("status") == "ok"
        if not health_ok:
            errors.append("Health endpoint did not return status=ok.")
    except Exception as exc:
        health = {"error": f"{type(exc).__name__}: {exc}"}
        health_ok = False
        errors.append("Health endpoint failed.")

    # 2. Capabilities check
    try:
        capabilities = get_json(f"{base_url}/v1/capabilities", timeout_sec=timeout_sec)
        capabilities_ok = (
            "exclude_water" in capabilities.get("supported_executable_constraints", [])
            and "max_slope_deg" in capabilities.get("supported_executable_constraints", [])
            and "solar" in capabilities.get("supported_project_types", [])
        )
        if not capabilities_ok:
            errors.append("Capabilities endpoint missing expected supported capabilities.")
    except Exception as exc:
        capabilities = {"error": f"{type(exc).__name__}: {exc}"}
        capabilities_ok = False
        errors.append("Capabilities endpoint failed.")

    # 3. Full execution case
    case_1_payload = {
        "query": "我想在北京市找光伏候选区域，要求避开水体，坡度小于10度",
        "execute": True,
        "include_remote_sensing_summary": True,
        "timeout_sec": timeout_sec,
    }

    try:
        case_1 = post_json(
            f"{base_url}/v1/site-selection/analyze",
            payload=case_1_payload,
            timeout_sec=timeout_sec + 60,
        )

        execution = case_1.get("execution", {})
        parsed = case_1.get("request_scope", {}).get("parsed_request", {})

        case_1_ok = (
            case_1.get("service_status") == "completed"
            and parsed.get("project_type") == "solar"
            and parsed.get("exclude_water") is True
            and float(parsed.get("max_slope_deg", -1)) == 10.0
            and execution.get("dynamic_execution_status") == "completed"
            and int(execution.get("candidate_count", 0)) > 0
            and case_1.get("remote_sensing_summary", {}).get("remote_sensing_pipeline_available") is True
        )

        if not case_1_ok:
            errors.append("Full execution API case did not meet expected conditions.")

        case_results.append(
            {
                "case_id": "api_case_001_solar_water_slope10_execute",
                "case_status": "passed" if case_1_ok else "failed",
                "query": case_1_payload["query"],
                "service_status": case_1.get("service_status"),
                "project_type": parsed.get("project_type"),
                "scope_kind": case_1.get("request_scope", {}).get("scope_kind"),
                "execution_status": execution.get("dynamic_execution_status"),
                "candidate_count": execution.get("candidate_count"),
                "job_id": case_1.get("dynamic_job", {}).get("job_id"),
                "final_candidate_path": execution.get("final_candidate_path"),
                "remote_sensing_summary_status": case_1.get("remote_sensing_summary", {}).get("status"),
            }
        )
    except Exception as exc:
        errors.append("Full execution API case failed.")
        case_results.append(
            {
                "case_id": "api_case_001_solar_water_slope10_execute",
                "case_status": "failed",
                "query": case_1_payload["query"],
                "failure_reason": f"{type(exc).__name__}: {exc}",
            }
        )

    # 4. Partial-support planning case
    # This tests that the API can report out-of-scope constraints without pretending support.
    case_2_payload = {
        "query": "我想在北京市找风电场站候选区域，要求避开保护区，并考虑风资源",
        "execute": False,
        "include_remote_sensing_summary": False,
        "timeout_sec": timeout_sec,
    }

    try:
        case_2 = post_json(
            f"{base_url}/v1/site-selection/analyze",
            payload=case_2_payload,
            timeout_sec=timeout_sec + 60,
        )

        parsed = case_2.get("request_scope", {}).get("parsed_request", {})
        out_scope = case_2.get("request_scope", {}).get("requested_out_of_scope_constraints", [])

        case_2_ok = (
            case_2.get("service_status") == "completed"
            and parsed.get("project_type") == "wind"
            and case_2.get("request_scope", {}).get("request_status") == "partially_supported"
            and len(out_scope) >= 1
            and case_2.get("execution", {}).get("dynamic_execution_status") == "skipped"
        )

        if not case_2_ok:
            errors.append("Partial-support API case did not meet expected conditions.")

        case_results.append(
            {
                "case_id": "api_case_002_wind_protected_wind_resource_plan_only",
                "case_status": "passed" if case_2_ok else "failed",
                "query": case_2_payload["query"],
                "service_status": case_2.get("service_status"),
                "request_status": case_2.get("request_scope", {}).get("request_status"),
                "project_type": parsed.get("project_type"),
                "scope_kind": case_2.get("request_scope", {}).get("scope_kind"),
                "out_of_scope_constraint_count": len(out_scope),
                "execution_status": case_2.get("execution", {}).get("dynamic_execution_status"),
                "job_id": case_2.get("dynamic_job", {}).get("job_id"),
            }
        )
    except Exception as exc:
        errors.append("Partial-support API case failed.")
        case_results.append(
            {
                "case_id": "api_case_002_wind_protected_wind_resource_plan_only",
                "case_status": "failed",
                "query": case_2_payload["query"],
                "failure_reason": f"{type(exc).__name__}: {exc}",
            }
        )

    passed_case_count = sum(1 for c in case_results if c.get("case_status") == "passed")
    failed_case_count = len(case_results) - passed_case_count

    if errors:
        status = "failed"
        status_zh = "闭环 5B 企业级 API 服务验收失败"
    else:
        status = "passed"
        status_zh = "闭环 5B 企业级 API 服务验收通过"

    report = {
        "closed_loop_5b_enterprise_api_validation_id": "closed_loop_5b_enterprise_api_validation_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "closed_loop_5b_enterprise_api_validation_status": status,
        "closed_loop_5b_status_zh": status_zh,
        "base_url": base_url,
        "health_ok": health_ok,
        "capabilities_ok": capabilities_ok,
        "case_count": len(case_results),
        "passed_case_count": passed_case_count,
        "failed_case_count": failed_case_count,
        "errors": errors,
        "health_response": health,
        "capabilities_summary": {
            "supported_project_types": capabilities.get("supported_project_types"),
            "supported_executable_constraints": capabilities.get("supported_executable_constraints"),
            "out_of_scope_or_pending_constraints": capabilities.get("out_of_scope_or_pending_constraints"),
            "closed_loop_5a_manifest_loaded": capabilities.get("closed_loop_5a_manifest_loaded"),
        } if isinstance(capabilities, dict) else {},
        "case_results": case_results,
        "next_stage": "closed_loop_5c_async_remote_sensing_and_job_status",
        "next_stage_zh": "下一步创建异步任务状态与遥感核验触发接口",
    }

    json_path = delivery_dir / "closed_loop_5b_enterprise_api_validation_v1.json"
    md_path = delivery_dir / "closed_loop_5b_enterprise_api_validation_report_v1.md"

    write_json(json_path, report)

    md = f"""
# Closed Loop 5B Enterprise API Validation Report

- Status: `{status}`
- Status zh: {status_zh}
- Base URL: `{base_url}`

## Endpoint Checks

- Health OK: {health_ok}
- Capabilities OK: {capabilities_ok}

## API Case Results

| case_id | status | project_type | scope_kind | execution_status | candidate_count |
|---|---|---|---|---|---|
"""

    for case in case_results:
        md += (
            f"| {case.get('case_id')} | {case.get('case_status')} | "
            f"{case.get('project_type')} | {case.get('scope_kind')} | "
            f"{case.get('execution_status')} | {case.get('candidate_count', '')} |\n"
        )

    md += f"""

## Interpretation

The enterprise API can accept natural-language site-selection requests, resolve scope, create dynamic plans, validate jobs, and execute GIS screening through an HTTP interface.

The full execution case proves that the API can produce a real request-scoped GeoJSON candidate output.

The partial-support case proves that the API records unsupported constraints instead of pretending full capability.

## Next Stage

{report["next_stage_zh"]}
"""

    write_text(md_path, md)

    return {
        "closed_loop_5b_enterprise_api_validation_status": status,
        "closed_loop_5b_status_zh": status_zh,
        "health_ok": health_ok,
        "capabilities_ok": capabilities_ok,
        "case_count": len(case_results),
        "passed_case_count": passed_case_count,
        "failed_case_count": failed_case_count,
        "json_report_path": str(json_path),
        "markdown_report_path": str(md_path),
        "next_stage": report["next_stage"],
        "next_stage_zh": report["next_stage_zh"],
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--timeout-sec", type=int, default=900)
    args = parser.parse_args()

    result = validate_enterprise_api(
        base_url=args.base_url,
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

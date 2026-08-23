"""Loop Engineering for request-driven renewable-site Agent.

Implements a real Plan -> Execute -> Verify -> Repair/Replan decision loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.agent.dynamic_plan_builder import build_dynamic_plan
from renewable_site_agent.agent.request_scope import parse_and_resolve
from renewable_site_agent.application.create_dynamic_request_job import (
    create_dynamic_request_job,
)
from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.run_dynamic_request_job import (
    run_dynamic_request_job,
)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _exists(path_text: str | None) -> bool:
    if not path_text:
        return False
    return Path(path_text).is_file()


def _make_repair_or_replan_decision(
    execution_result: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    status = execution_result.get("dynamic_execution_status")
    failure_reason = execution_result.get("failure_reason")
    out_of_scope = scope.get("requested_out_of_scope_constraints", [])

    if status == "completed":
        return {
            "decision": "no_repair_needed",
            "decision_zh": "无需修复",
            "reason_zh": "当前请求范围已完整执行并验证通过。",
        }

    if status == "completed_with_out_of_scope_constraints":
        return {
            "decision": "no_repair_needed_scope_boundary_recorded",
            "decision_zh": "无需修复，范围外约束已记录",
            "reason_zh": "当前可执行范围已完成；用户提到但当前版本不支持的约束已进入范围外记录。",
            "out_of_scope_constraints": out_of_scope,
            "future_action": "create_new_plan_version_when_capability_is_added",
            "future_action_zh": "未来接入对应能力后创建新计划版本，不覆盖当前结果。",
        }

    if failure_reason:
        lower_reason = str(failure_reason).lower()

        if "missing" in lower_reason or "cannot find" in lower_reason:
            return {
                "decision": "manual_data_fix_required",
                "decision_zh": "需要人工补齐数据后恢复",
                "reason_zh": "失败原因指向缺失数据或找不到文件，不能自动伪造结果。",
                "failure_reason": failure_reason,
                "automatic_retry_allowed": False,
            }

        if "invalid" in lower_reason or "datasourceerror" in lower_reason:
            return {
                "decision": "artifact_format_repair_required",
                "decision_zh": "需要修复产物格式后重试",
                "reason_zh": "失败原因指向数据格式或产物格式问题，应修复适配器或源文件后重跑。",
                "failure_reason": failure_reason,
                "automatic_retry_allowed": False,
            }

    return {
        "decision": "replan_or_manual_inspection_required",
        "decision_zh": "需要重规划或人工检查",
        "reason_zh": "当前失败原因无法安全自动修复。",
        "failure_reason": failure_reason,
        "automatic_retry_allowed": False,
    }


def run_request_loop(request_text: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    context_path = RUNTIME_ROOT / "context" / "request_driven_agent_context_package_v1.json"
    skill_registry_path = RUNTIME_ROOT / "skills" / "renewable_site_agent_skill_registry_v1.json"
    tool_schema_path = RUNTIME_ROOT / "tool_protocol" / "internal_tool_schema_registry_v1.json"

    context_loaded = context_path.is_file()
    skill_registry_loaded = skill_registry_path.is_file()
    tool_schema_loaded = tool_schema_path.is_file()

    loop_phases: list[dict[str, Any]] = []

    loop_phases.append(
        {
            "phase": "context_load",
            "phase_zh": "加载压缩上下文和工程资产",
            "status": "completed",
            "context_loaded": context_loaded,
            "skill_registry_loaded": skill_registry_loaded,
            "tool_schema_loaded": tool_schema_loaded,
        }
    )

    scope = parse_and_resolve(request_text)
    plan = build_dynamic_plan(scope)
    request_id = scope["request_id"]

    scope_path = RUNTIME_ROOT / "request_scopes" / f"{request_id}_scope.json"
    plan_path = RUNTIME_ROOT / "request_plans" / f"{request_id}_dynamic_agent_plan.json"

    write_json(scope_path, scope)
    write_json(plan_path, plan)

    loop_phases.append(
        {
            "phase": "plan",
            "phase_zh": "规划",
            "status": "completed",
            "request_id": request_id,
            "request_status": scope.get("request_status"),
            "project_type": scope["parsed_request"].get("project_type"),
            "scope_kind": scope.get("scope_kind"),
            "planned_step_count": plan.get("planned_step_count"),
            "scope_path": str(scope_path),
            "plan_path": str(plan_path),
        }
    )

    job_result = create_dynamic_request_job(plan_path)

    loop_phases.append(
        {
            "phase": "job_create",
            "phase_zh": "创建动态任务",
            "status": "completed",
            "job_id": job_result.get("job_id"),
            "job_status": job_result.get("job_status"),
            "manifest_path": job_result.get("manifest_path"),
        }
    )

    execution_result = run_dynamic_request_job(job_result["job_id"])

    loop_phases.append(
        {
            "phase": "execute",
            "phase_zh": "执行",
            "status": "completed"
            if execution_result.get("failure_reason") is None
            else "failed_or_blocked",
            "dynamic_execution_status": execution_result.get("dynamic_execution_status"),
            "candidate_count": execution_result.get("candidate_count"),
            "execution_path": execution_result.get("execution_path"),
            "final_candidate_path": execution_result.get("final_candidate_path"),
            "failure_reason": execution_result.get("failure_reason"),
        }
    )

    verification_checks = {
        "candidate_count_is_integer": isinstance(
            execution_result.get("candidate_count"), int
        ),
        "final_candidate_file_exists": _exists(
            execution_result.get("final_candidate_path")
        ),
        "execution_record_exists": _exists(execution_result.get("execution_path")),
        "status_is_acceptable": execution_result.get("dynamic_execution_status")
        in {"completed", "completed_with_out_of_scope_constraints"},
        "failure_reason_is_null": execution_result.get("failure_reason") is None,
    }

    verification_status = (
        "passed" if all(verification_checks.values()) else "failed"
    )

    loop_phases.append(
        {
            "phase": "verify",
            "phase_zh": "验证",
            "status": verification_status,
            "checks": verification_checks,
        }
    )

    repair_decision = _make_repair_or_replan_decision(execution_result, scope)

    loop_phases.append(
        {
            "phase": "repair_or_replan_decision",
            "phase_zh": "修复或重规划决策",
            "status": "completed",
            "decision": repair_decision,
        }
    )

    if verification_status == "passed":
        if execution_result.get("dynamic_execution_status") == "completed":
            loop_status = "completed"
            loop_status_zh = "循环完成"
        else:
            loop_status = "completed_with_scope_boundary"
            loop_status_zh = "当前可执行范围完成，范围外约束已记录"
    else:
        loop_status = "failed_requires_repair_or_replan"
        loop_status_zh = "循环失败，需要修复或重规划"

    loop_record = {
        "loop_id": f"request_loop_{request_id}_v1",
        "created_at_utc": now,
        "loop_mode": "plan_execute_verify_repair_replan",
        "loop_mode_zh": "规划-执行-验证-修复/重规划循环",
        "request_text": request_text,
        "request_id": request_id,
        "job_id": job_result.get("job_id"),
        "loop_status": loop_status,
        "loop_status_zh": loop_status_zh,
        "project_type": scope["parsed_request"].get("project_type"),
        "project_type_zh": scope["parsed_request"].get("project_type_zh"),
        "scope_kind": scope.get("scope_kind"),
        "request_status": scope.get("request_status"),
        "plan_status": plan.get("plan_status"),
        "dynamic_execution_status": execution_result.get("dynamic_execution_status"),
        "candidate_count": execution_result.get("candidate_count"),
        "final_candidate_path": execution_result.get("final_candidate_path"),
        "repair_or_replan_decision": repair_decision,
        "loop_phases": loop_phases,
    }

    loop_path = RUNTIME_ROOT / "loops" / f"{request_id}_loop_run_v1.json"
    write_json(loop_path, loop_record)

    return {
        "loop_status": loop_status,
        "loop_status_zh": loop_status_zh,
        "request_id": request_id,
        "job_id": job_result.get("job_id"),
        "project_type": loop_record["project_type"],
        "scope_kind": loop_record["scope_kind"],
        "dynamic_execution_status": loop_record["dynamic_execution_status"],
        "candidate_count": loop_record["candidate_count"],
        "repair_or_replan_decision": repair_decision["decision"],
        "loop_record_path": str(loop_path),
    }


def run_loop_harness() -> dict[str, Any]:
    cases = [
        {
            "case_id": "loop_case_001_water_only",
            "request_text": "我想在北京市找新能源场站候选区域，要求避开水体",
        },
        {
            "case_id": "loop_case_002_protected_with_out_of_scope",
            "request_text": "我想在北京市找风电场站候选区域，要求避开保护区，并考虑风资源",
        },
        {
            "case_id": "loop_case_003_arbitrary_slope_12_5",
            "request_text": "我想在北京市找新能源场站候选区域，要求避开水体，坡度小于12.5度",
        },
    ]

    results = []
    for case in cases:
        try:
            result = run_request_loop(case["request_text"])
            result["case_id"] = case["case_id"]
            result["case_status"] = "passed" if result["loop_status"] in {
                "completed",
                "completed_with_scope_boundary",
            } else "failed"
        except Exception as exc:
            result = {
                "case_id": case["case_id"],
                "case_status": "failed",
                "exception": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)

    passed_count = sum(1 for item in results if item.get("case_status") == "passed")
    failed_count = len(results) - passed_count

    report = {
        "loop_harness_id": "closed_loop_3c_loop_engineering_harness_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "loop_harness_status": "passed" if failed_count == 0 else "failed",
        "total_case_count": len(results),
        "passed_case_count": passed_count,
        "failed_case_count": failed_count,
        "case_results": results,
    }

    report_path = RUNTIME_ROOT / "loops" / "closed_loop_3c_loop_engineering_harness_v1.json"
    write_json(report_path, report)

    return {
        "loop_harness_status": report["loop_harness_status"],
        "total_case_count": report["total_case_count"],
        "passed_case_count": report["passed_case_count"],
        "failed_case_count": report["failed_case_count"],
        "loop_harness_report_path": str(report_path),
        "case_summaries": [
            {
                "case_id": item.get("case_id"),
                "case_status": item.get("case_status"),
                "loop_status": item.get("loop_status"),
                "project_type": item.get("project_type"),
                "scope_kind": item.get("scope_kind"),
                "dynamic_execution_status": item.get("dynamic_execution_status"),
                "candidate_count": item.get("candidate_count"),
                "repair_or_replan_decision": item.get("repair_or_replan_decision"),
                "exception": item.get("exception"),
            }
            for item in results
        ],
    }

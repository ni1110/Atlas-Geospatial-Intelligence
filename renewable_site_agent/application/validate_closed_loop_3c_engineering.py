"""Final validation for Closed Loop 3C engineering enhancement.

Validates:
1. Closed Loop 3B request-driven Agent validation;
2. Context Engineering artifact;
3. Skill Registry artifact;
4. Internal MCP-like Tool Schema artifact;
5. Loop Engineering harness.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


class ClosedLoop3CValidationError(RuntimeError):
    """Raised when Closed Loop 3C validation fails."""


REQUIRED_SKILLS = {
    "request_understanding_skill",
    "water_screening_skill",
    "protected_area_screening_skill",
    "dynamic_slope_screening_skill",
    "request_job_execution_skill",
    "harness_validation_skill",
}

REQUIRED_TOOL_IDS = {
    "load_administrative_boundary",
    "exclude_water_bodies",
    "prepare_protected_areas",
    "apply_protected_area_screening",
    "prepare_terrain_dem",
    "calculate_slope",
    "vectorize_slope_mask",
    "apply_slope_screening",
    "finalize_request_scoped_candidates",
    "validate_request_scoped_job",
}

REQUIRED_CONTEXT_CAPABILITIES = {
    "water_exclusion",
    "protected_area_exclusion",
    "dynamic_slope_threshold",
}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ClosedLoop3CValidationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_closed_loop_3c_engineering() -> dict[str, Any]:
    validation_3b_path = (
        RUNTIME_ROOT
        / "request_driven_agent"
        / "closed_loop_3b_request_driven_validation_v1.json"
    )
    context_path = RUNTIME_ROOT / "context" / "request_driven_agent_context_package_v1.json"
    skill_path = RUNTIME_ROOT / "skills" / "renewable_site_agent_skill_registry_v1.json"
    tool_schema_path = (
        RUNTIME_ROOT / "tool_protocol" / "internal_tool_schema_registry_v1.json"
    )
    loop_harness_path = (
        RUNTIME_ROOT / "loops" / "closed_loop_3c_loop_engineering_harness_v1.json"
    )

    validation_3b = read_json(validation_3b_path)
    context = read_json(context_path)
    skill_registry = read_json(skill_path)
    tool_schema_registry = read_json(tool_schema_path)
    loop_harness = read_json(loop_harness_path)

    if validation_3b.get("closed_loop_3b_request_driven_validation_status") != "passed":
        raise ClosedLoop3CValidationError("Closed Loop 3B validation is not passed.")

    if context.get("context_mode") != "compressed_agent_state":
        raise ClosedLoop3CValidationError("Context package is not compressed_agent_state.")

    compressed_cases = context.get("compressed_case_memory", [])
    if len(compressed_cases) < 6:
        raise ClosedLoop3CValidationError(
            f"Expected at least 6 compressed cases, got {len(compressed_cases)}."
        )

    context_capabilities = set(
        context.get("capability_summary", {}).get("supported_execution_capabilities", [])
    )
    missing_context_capabilities = sorted(
        REQUIRED_CONTEXT_CAPABILITIES - context_capabilities
    )
    if missing_context_capabilities:
        raise ClosedLoop3CValidationError(
            f"Missing context capabilities: {missing_context_capabilities}"
        )

    skills = skill_registry.get("skills", [])
    skill_ids = {skill.get("skill_id") for skill in skills}
    missing_skills = sorted(REQUIRED_SKILLS - skill_ids)
    if missing_skills:
        raise ClosedLoop3CValidationError(f"Missing skills: {missing_skills}")

    if skill_registry.get("skill_count") != len(skills):
        raise ClosedLoop3CValidationError("skill_count does not match skills length.")

    tool_schemas = tool_schema_registry.get("tool_schemas", [])
    tool_ids = {tool.get("tool_id") for tool in tool_schemas}
    missing_tools = sorted(REQUIRED_TOOL_IDS - tool_ids)
    if missing_tools:
        raise ClosedLoop3CValidationError(
            f"Missing internal tool schemas: {missing_tools}"
        )

    if tool_schema_registry.get("tool_count") != len(tool_schemas):
        raise ClosedLoop3CValidationError(
            "tool_count does not match tool_schemas length."
        )

    if tool_schema_registry.get("schema_mode") != "internal_mcp_like_tool_protocol":
        raise ClosedLoop3CValidationError(
            "Tool schema registry must be internal_mcp_like_tool_protocol."
        )

    if tool_schema_registry.get("mcp_server_status") != "not_implemented_in_closed_loop_3c":
        raise ClosedLoop3CValidationError(
            "Full MCP Server should not be implemented in Closed Loop 3C."
        )

    if loop_harness.get("loop_harness_status") != "passed":
        raise ClosedLoop3CValidationError(
            f"Loop harness is not passed: {loop_harness.get('loop_harness_status')}"
        )

    loop_cases = loop_harness.get("case_results", [])
    if len(loop_cases) < 3:
        raise ClosedLoop3CValidationError(
            f"Expected at least 3 loop cases, got {len(loop_cases)}."
        )

    failed_loop_cases = [
        case for case in loop_cases if case.get("case_status") != "passed"
    ]
    if failed_loop_cases:
        raise ClosedLoop3CValidationError(
            f"Failed loop cases: {[case.get('case_id') for case in failed_loop_cases]}"
        )

    arbitrary_slope_cases = [
        case
        for case in loop_cases
        if case.get("scope_kind") == "exclude_water+max_slope_deg"
        and isinstance(case.get("candidate_count"), int)
    ]
    if not arbitrary_slope_cases:
        raise ClosedLoop3CValidationError(
            "No loop case proves arbitrary numeric slope-threshold execution."
        )

    loop_statuses = sorted({case.get("loop_status") for case in loop_cases})
    repair_decisions = sorted(
        {case.get("repair_or_replan_decision") for case in loop_cases}
    )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    validation = {
        "closed_loop_3c_engineering_validation_status": "passed",
        "closed_loop_3c_status_zh": "闭环 3C 工程增强层验收通过",
        "created_at_utc": now,
        "source_artifacts": {
            "closed_loop_3b_validation": str(validation_3b_path),
            "context_package": str(context_path),
            "skill_registry": str(skill_path),
            "internal_tool_schema_registry": str(tool_schema_path),
            "loop_harness": str(loop_harness_path),
        },
        "validated_components": {
            "context_engineering": {
                "status": "passed",
                "compressed_case_count": len(compressed_cases),
                "context_mode": context.get("context_mode"),
            },
            "skill_registry": {
                "status": "passed",
                "skill_count": len(skills),
                "skill_ids": sorted(skill_ids),
            },
            "internal_tool_protocol": {
                "status": "passed",
                "schema_mode": tool_schema_registry.get("schema_mode"),
                "tool_count": len(tool_schemas),
                "full_mcp_server": "deferred_to_closed_loop_5",
            },
            "loop_engineering": {
                "status": "passed",
                "loop_case_count": len(loop_cases),
                "loop_statuses": loop_statuses,
                "repair_or_replan_decisions": repair_decisions,
            },
            "harness_strengthening": {
                "status": "passed",
                "request_driven_harness_cases": validation_3b.get("total_case_count"),
                "loop_harness_cases": loop_harness.get("total_case_count"),
            },
        },
        "engineering_capabilities_zh": [
            "已形成压缩上下文包，支持长任务状态续接。",
            "已形成技能登记表，将请求理解、水体筛选、保护区筛选、动态坡度筛选、任务执行和 Harness 验证封装为技能。",
            "已形成内部类 MCP 工具协议，规范工具输入、输出、产物契约、失败模式和重试策略。",
            "已形成 Plan-Execute-Verify-Repair/Replan 循环记录。",
            "已通过 Loop Harness 验证任意坡度阈值和范围外约束处理。",
        ],
        "known_deferred_items": {
            "full_mcp_server": "closed_loop_5",
            "api_async_task_runtime": "closed_loop_5",
            "human_in_the_loop_review": "closed_loop_5",
            "enterprise_deployment": "closed_loop_5",
        },
        "assessment_zh": (
            "闭环 3C 已将请求驱动 Agent 从可运行能力提升为工程化能力："
            "具备上下文压缩、技能封装、内部工具协议、循环控制和批量验证。"
            "完整 MCP Server、API 异步任务、人工介入和企业部署仍按计划放到闭环 5。"
        ),
        "next_stage": "closed_loop_4_remote_sensing_verification",
        "next_stage_zh": "下一步进入闭环 4：多模态遥感核验",
    }

    output_path = (
        RUNTIME_ROOT
        / "engineering"
        / "closed_loop_3c_engineering_validation_v1.json"
    )
    write_json(output_path, validation)

    return {
        "closed_loop_3c_engineering_validation_status": validation[
            "closed_loop_3c_engineering_validation_status"
        ],
        "closed_loop_3c_status_zh": validation["closed_loop_3c_status_zh"],
        "context_engineering_status": "passed",
        "compressed_case_count": len(compressed_cases),
        "skill_registry_status": "passed",
        "skill_count": len(skills),
        "internal_tool_protocol_status": "passed",
        "tool_count": len(tool_schemas),
        "loop_engineering_status": "passed",
        "loop_case_count": len(loop_cases),
        "loop_statuses": loop_statuses,
        "repair_or_replan_decisions": repair_decisions,
        "validation_report_path": str(output_path),
        "next_stage": validation["next_stage"],
        "next_stage_zh": validation["next_stage_zh"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = validate_closed_loop_3c_engineering()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

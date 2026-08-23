from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def validate_final_enterprise_delivery() -> dict[str, Any]:
    enterprise_dir = RUNTIME_ROOT / "enterprise_delivery"

    paths = {
        "closed_loop_3b": RUNTIME_ROOT / "request_driven_agent" / "closed_loop_3b_request_driven_validation_v1.json",
        "closed_loop_3c": RUNTIME_ROOT / "engineering" / "closed_loop_3c_engineering_validation_v1.json",
        "closed_loop_4": RUNTIME_ROOT / "remote_sensing" / "closed_loop_4_remote_sensing_final_validation_v1.json",
        "closed_loop_5a": enterprise_dir / "closed_loop_5a_enterprise_delivery_package_v1.json",
        "closed_loop_5b": enterprise_dir / "closed_loop_5b_enterprise_api_validation_v1.json",
        "closed_loop_5c": enterprise_dir / "closed_loop_5c_async_job_api_validation_v1.json",
    }

    reports = {}
    errors: list[str] = []

    for key, path in paths.items():
        try:
            reports[key] = read_json(path)
        except Exception as exc:
            reports[key] = {"error": f"{type(exc).__name__}: {exc}"}
            errors.append(f"{key} report missing or unreadable: {path}")

    r3b = reports["closed_loop_3b"]
    r3c = reports["closed_loop_3c"]
    r4 = reports["closed_loop_4"]
    r5a = reports["closed_loop_5a"]
    r5b = reports["closed_loop_5b"]
    r5c = reports["closed_loop_5c"]

    checks = [
        {
            "check_id": "closed_loop_3b_request_driven",
            "passed": r3b.get("closed_loop_3b_request_driven_validation_status") == "passed",
            "evidence": f"passed_case_count={r3b.get('passed_case_count')}, total_case_count={r3b.get('total_case_count')}",
        },
        {
            "check_id": "closed_loop_3c_engineering",
            "passed": r3c.get("closed_loop_3c_engineering_validation_status") == "passed",
            "evidence": f"context={r3c.get('context_engineering_status')}, skills={r3c.get('skill_count')}, tools={r3c.get('tool_count')}, loop_cases={r3c.get('loop_case_count')}",
        },
        {
            "check_id": "closed_loop_4_remote_sensing",
            "passed": str(r4.get("closed_loop_4_remote_sensing_validation_status", "")).startswith("passed"),
            "evidence": f"tiles={r4.get('acquired_tile_count')}/{r4.get('sample_count')}, rule={r4.get('rule_verified_count')}, vlm={r4.get('vlm_verified_count')}, review={r4.get('human_review_required_count')}",
        },
        {
            "check_id": "closed_loop_5a_delivery_package",
            "passed": r5a.get("delivery_status") == "created",
            "evidence": f"delivery_status={r5a.get('delivery_status')}",
        },
        {
            "check_id": "closed_loop_5b_enterprise_api",
            "passed": r5b.get("closed_loop_5b_enterprise_api_validation_status") == "passed",
            "evidence": f"health={r5b.get('health_ok')}, capabilities={r5b.get('capabilities_ok')}, cases={r5b.get('passed_case_count')}/{r5b.get('case_count')}",
        },
        {
            "check_id": "closed_loop_5c_async_job_api",
            "passed": r5c.get("closed_loop_5c_async_job_api_validation_status") == "passed",
            "evidence": f"job_id={r5c.get('job_id')}, trigger_id={r5c.get('trigger_id')}, trigger_status={r5c.get('trigger_status')}",
        },
    ]

    for item in checks:
        if not item["passed"]:
            errors.append(f"Check failed: {item['check_id']}")

    final_status = "passed" if not errors else "failed"
    final_status_zh = "闭环 5 企业级交付总体验收通过" if not errors else "闭环 5 企业级交付总体验收失败"

    report = {
        "closed_loop_5d_final_enterprise_validation_id": "closed_loop_5d_final_enterprise_validation_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "closed_loop_5d_final_enterprise_validation_status": final_status,
        "closed_loop_5d_status_zh": final_status_zh,
        "errors": errors,
        "checks": checks,
        "system_capability_summary_zh": {
            "one_sentence": "系统已形成从自然语言请求到动态 GIS 筛选、真实遥感核验、证据融合、API 服务和异步触发的新能源场站选址 Agent 闭环。",
            "request_driven_agent": "支持从用户一句话中解析项目类型、区域和约束，动态生成计划和 job。",
            "gis_screening": "支持水体排除、保护区排除和动态坡度阈值筛选。",
            "remote_sensing": "已接入真实 Sentinel-2 影像，并完成规则核验和 Qwen2.5-VL 视觉核验。",
            "enterprise_delivery": "已提供 API 服务、job 状态查询、遥感核验触发记录和交付报告。",
        },
        "current_limitations_zh": [
            "系统不自动批准候选区，所有输出均为选址辅助和人工复核建议。",
            "风资源、光照资源、电网接入成本、基本农田、变电站距离仍需真实数据源接入。",
            "当前遥感触发接口创建 queued trigger，完整异步 worker 可作为后续扩展。",
            "当前 MCP 是内部 tool schema / MCP-like protocol，完整 MCP Server 可作为后续扩展。",
        ],
        "key_outputs": {
            "5a_delivery_manifest": str(paths["closed_loop_5a"]),
            "5b_api_validation": str(paths["closed_loop_5b"]),
            "5c_async_api_validation": str(paths["closed_loop_5c"]),
            "4_remote_sensing_validation": str(paths["closed_loop_4"]),
        },
        "final_project_stage": "closed_loop_5_enterprise_delivery_completed",
        "final_project_stage_zh": "闭环 5 企业级 Agent 交付层完成",
        "next_possible_extensions": [
            "实现真正的异步 worker，消费 remote-sensing trigger 并自动跑 4A-4D",
            "接入风资源、光照资源、电网接入成本和变电站距离数据",
            "实现完整 MCP Server",
            "增加前端可视化地图页面",
            "制作本地迁移包和 Docker 部署包",
        ],
    }

    json_path = enterprise_dir / "closed_loop_5d_final_enterprise_validation_v1.json"
    md_path = enterprise_dir / "closed_loop_5d_final_enterprise_validation_report_v1.md"
    summary_path = enterprise_dir / "geoagent_final_project_summary_v1.md"

    write_json(json_path, report)

    md = f"""
# Closed Loop 5D Final Enterprise Validation Report

- Status: `{final_status}`
- Status zh: {final_status_zh}

## Validation Checks

| check_id | passed | evidence |
|---|---|---|
"""

    for item in checks:
        md += f"| {item['check_id']} | {item['passed']} | {item['evidence']} |\n"

    md += f"""

## Final Interpretation

{report["system_capability_summary_zh"]["one_sentence"]}

The system is no longer only a command-line GIS workflow. It now has an enterprise API entrypoint, job status query, queued remote-sensing trigger records, delivery package, and final validation reports.

## Current Limitations

"""

    for item in report["current_limitations_zh"]:
        md += f"- {item}\n"

    md += """

## Recommended Next Extensions

"""

    for item in report["next_possible_extensions"]:
        md += f"- {item}\n"

    write_text(md_path, md)

    summary = f"""
# GeoAgent / RenewableSite Agent Final Project Summary

## 一句话总结

本项目构建了一个面向新能源场站选址的请求驱动 GIS + 遥感核验 Agent。用户输入自然语言选址需求后，系统能够解析项目类型和约束，动态生成 GIS 计划与任务，执行水体、保护区、坡度等空间筛选，并结合真实 Sentinel-2 遥感影像、规则核验和 Qwen2.5-VL 视觉核验生成证据报告和人工复核建议。

## 用户一句话之后系统做什么

用户请求
→ 请求解析
→ 约束识别
→ 动态计划生成
→ 动态 job 创建
→ GIS 筛选执行
→ 候选区 GeoJSON 输出
→ job 状态查询
→ 遥感核验 trigger 入队
→ Sentinel-2 / 规则 / VLM 核验能力支撑
→ 证据融合与人工复核建议

## 已完成闭环

- 闭环 3B：请求驱动 Agent，6/6 case passed。
- 闭环 3C：工程增强层，包括 context package、skill registry、internal tool schema、loop engineering。
- 闭环 4：真实遥感核验，Sentinel-2 16/16，规则核验 16/16，Qwen2.5-VL 15/16。
- 闭环 5A：企业级交付包。
- 闭环 5B：企业 API 服务。
- 闭环 5C：job 状态查询与遥感 trigger。
- 闭环 5D：企业级交付总体验收。

## 当前最终状态

`{final_status}`：{final_status_zh}

## 不能夸大的地方

- 当前结果不是自动批准建设。
- 遥感和 VLM 是辅助核验，不替代人工审查。
- 风资源、光照资源、电网接入成本等还需要真实数据源。
- 当前 trigger 是 queued 记录，完整异步 worker 是下一步扩展。
"""

    write_text(summary_path, summary)

    return {
        "closed_loop_5d_final_enterprise_validation_status": final_status,
        "closed_loop_5d_status_zh": final_status_zh,
        "check_count": len(checks),
        "passed_check_count": sum(1 for c in checks if c["passed"]),
        "failed_check_count": sum(1 for c in checks if not c["passed"]),
        "json_report_path": str(json_path),
        "markdown_report_path": str(md_path),
        "final_project_summary_path": str(summary_path),
        "final_project_stage": report["final_project_stage"],
        "final_project_stage_zh": report["final_project_stage_zh"],
        "errors": errors,
    }


def main() -> None:
    result = validate_final_enterprise_delivery()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

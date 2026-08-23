from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    path.write_text(content.strip() + "\n", encoding="utf-8")


def create_delivery_package() -> dict[str, Any]:
    report_3b = RUNTIME_ROOT / "request_driven_agent" / "closed_loop_3b_request_driven_validation_v1.json"
    report_3c = RUNTIME_ROOT / "engineering" / "closed_loop_3c_engineering_validation_v1.json"
    report_4 = RUNTIME_ROOT / "remote_sensing" / "closed_loop_4_remote_sensing_final_validation_v1.json"
    report_4d = RUNTIME_ROOT / "remote_sensing" / "closed_loop_4d_remote_sensing_evidence_fusion_v1.json"

    v3b = read_json(report_3b)
    v3c = read_json(report_3c)
    v4 = read_json(report_4)
    v4d = read_json(report_4d)

    delivery_dir = RUNTIME_ROOT / "enterprise_delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)

    api_contract = {
        "api_contract_id": "geoagent_enterprise_api_contract_v1",
        "service_name": "RenewableSite Agent",
        "service_name_zh": "新能源场站选址 Agent",
        "entrypoint": {
            "method": "POST",
            "path": "/v1/site-selection/analyze",
            "input": {
                "query": "用户自然语言选址请求",
                "example": "我想在北京市找光伏候选区域，要求避开水体，坡度小于10度。"
            },
            "output": {
                "request_status": "supported / partially_supported / unsupported",
                "job_id": "动态任务 ID",
                "gis_candidate_summary": "GIS 候选区摘要",
                "remote_sensing_summary": "遥感核验摘要",
                "human_review_required": "是否需要人工复核",
                "evidence_report_paths": "证据报告路径"
            }
        },
        "supported_capabilities": [
            "自然语言请求解析",
            "动态 GIS plan 生成",
            "动态 job 执行",
            "水体排除",
            "保护区排除",
            "动态坡度阈值筛选",
            "Sentinel-2 真实遥感影像下载",
            "规则遥感核验",
            "Qwen2.5-VL 真实视觉核验",
            "规则 + VLM 证据融合",
            "人工复核标记",
            "审计报告生成"
        ],
        "known_boundaries": [
            "不自动批准候选区",
            "不伪造遥感影像",
            "不伪造 VLM 结论",
            "风资源、光照资源、电网接入成本、基本农田、变电站距离仍需真实数据源接入",
            "当前是内部 tool schema / MCP-like protocol，完整 MCP Server 放在后续闭环 5B/5C"
        ]
    }

    manifest = {
        "delivery_package_id": "closed_loop_5a_enterprise_delivery_package_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "delivery_status": "created",
        "delivery_status_zh": "闭环 5A 企业级交付包已创建",
        "project_root": str(PROJECT_ROOT),
        "runtime_root": str(RUNTIME_ROOT),
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform()
        },
        "closed_loop_3b": {
            "status": v3b.get("closed_loop_3b_request_driven_validation_status"),
            "total_case_count": v3b.get("total_case_count"),
            "passed_case_count": v3b.get("passed_case_count"),
            "project_types_covered": v3b.get("project_types_covered"),
            "scope_kinds_covered": v3b.get("scope_kinds_covered"),
            "report_path": str(report_3b)
        },
        "closed_loop_3c": {
            "status": v3c.get("closed_loop_3c_engineering_validation_status"),
            "context_engineering_status": v3c.get("context_engineering_status"),
            "skill_count": v3c.get("skill_count"),
            "tool_count": v3c.get("tool_count"),
            "loop_case_count": v3c.get("loop_case_count"),
            "report_path": str(report_3c)
        },
        "closed_loop_4": {
            "status": v4.get("closed_loop_4_remote_sensing_validation_status"),
            "sample_count": v4.get("sample_count"),
            "acquired_tile_count": v4.get("acquired_tile_count"),
            "rule_verified_count": v4.get("rule_verified_count"),
            "vlm_verified_count": v4.get("vlm_verified_count"),
            "vlm_failed_count": v4.get("vlm_failed_count"),
            "human_review_required_count": v4.get("human_review_required_count"),
            "real_imagery_source": v4.get("real_imagery_source"),
            "vlm_model": v4.get("vlm_model"),
            "report_path": str(report_4)
        },
        "closed_loop_4d": {
            "fusion_status": v4d.get("fusion_status"),
            "final_favorable_count": v4d.get("final_favorable_count"),
            "final_uncertain_count": v4d.get("final_uncertain_count"),
            "final_unfavorable_count": v4d.get("final_unfavorable_count"),
            "report_path": str(report_4d)
        },
        "outputs": {
            "manifest": str(delivery_dir / "closed_loop_5a_enterprise_delivery_package_v1.json"),
            "api_contract": str(delivery_dir / "geoagent_enterprise_api_contract_v1.json"),
            "one_page_summary": str(delivery_dir / "geoagent_one_page_summary_v1.md"),
            "operator_runbook": str(delivery_dir / "geoagent_operator_runbook_v1.md")
        },
        "next_stage": "closed_loop_5b_enterprise_api_service",
        "next_stage_zh": "下一步创建企业级 API 服务入口"
    }

    one_page_summary = f"""
# RenewableSite Agent One-page Summary

## 项目定位

RenewableSite Agent 是一个面向新能源场站选址的请求驱动 GIS + 遥感核验 Agent。

它不是固定脚本，也不是让大模型直接编答案。系统会从自然语言请求中解析项目类型、区域和约束，动态生成 GIS 执行计划，调用真实 GIS 工具筛选候选区，并基于真实 Sentinel-2 遥感影像和 Qwen2.5-VL 做二次核验。

## 用户一句话后的执行链

用户请求
→ 请求解析
→ 约束识别
→ 动态计划
→ 动态 job
→ GIS 筛选
→ 候选区抽样
→ Sentinel-2 真实影像下载
→ 规则遥感核验
→ Qwen2.5-VL 视觉核验
→ 证据融合
→ 输出候选区与人工复核建议

## 当前完成结果

- 闭环 3B 请求驱动 Agent：{v3b.get("passed_case_count")} / {v3b.get("total_case_count")} cases passed
- 闭环 3C 工程增强层：{v3c.get("closed_loop_3c_engineering_validation_status")}
- 闭环 4 遥感核验：{v4.get("closed_loop_4_remote_sensing_validation_status")}
- Sentinel-2 真实影像：{v4.get("acquired_tile_count")} / {v4.get("sample_count")}
- 规则核验：{v4.get("rule_verified_count")} / {v4.get("sample_count")}
- VLM 核验：{v4.get("vlm_verified_count")} / {v4.get("sample_count")}
- 人工复核样本：{v4.get("human_review_required_count")}

## 关键边界

- 不伪造遥感影像。
- 不伪造 VLM 结论。
- 不自动批准候选区。
- 风资源、光照资源、电网接入成本等能力仍需后续真实数据源接入。
"""

    operator_runbook = r"""
# RenewableSite Agent Operator Runbook

## 1. 进入主环境

Set-Location <project-root-on-D-drive>
.\.venv-agent\Scripts\Activate.ps1

## 2. 验证闭环 3B

PYTHONPATH=. python -m renewable_site_agent.application.validate_closed_loop_3b_request_driven

## 3. 验证闭环 3C

PYTHONPATH=. python -m renewable_site_agent.application.validate_closed_loop_3c_engineering

## 4. 验证闭环 4

PYTHONPATH=. python -m renewable_site_agent.application.validate_closed_loop_4_remote_sensing

## 5. 启动 VLM 服务

Set-Location <project-root-on-D-drive>
.\.venv-vlm\Scripts\Activate.ps1
$env:CUDA_VISIBLE_DEVICES="0"
uvicorn vlm_openai_server:app --host 127.0.0.1 --port 8000

## 6. 配置 VLM 调用

$env:GEOAGENT_VLM_BASE_URL="http://127.0.0.1:8000/v1"
$env:GEOAGENT_VLM_MODEL="Qwen2.5-VL-7B-Instruct"
$env:GEOAGENT_VLM_API_KEY="EMPTY"

## 7. 重新运行 VLM 核验

PYTHONPATH=. python -m renewable_site_agent.application.run_closed_loop_4c_vlm_verification --timeout-sec 240

## 8. 重新运行证据融合

PYTHONPATH=. python -m renewable_site_agent.application.run_closed_loop_4d_evidence_fusion
"""

    write_json(delivery_dir / "geoagent_enterprise_api_contract_v1.json", api_contract)
    write_json(delivery_dir / "closed_loop_5a_enterprise_delivery_package_v1.json", manifest)
    write_text(delivery_dir / "geoagent_one_page_summary_v1.md", one_page_summary)
    write_text(delivery_dir / "geoagent_operator_runbook_v1.md", operator_runbook)

    return {
        "closed_loop_5a_delivery_package_status": "created",
        "closed_loop_5a_status_zh": "闭环 5A 企业级交付包已创建",
        "delivery_dir": str(delivery_dir),
        "delivery_manifest_path": str(delivery_dir / "closed_loop_5a_enterprise_delivery_package_v1.json"),
        "api_contract_path": str(delivery_dir / "geoagent_enterprise_api_contract_v1.json"),
        "one_page_summary_path": str(delivery_dir / "geoagent_one_page_summary_v1.md"),
        "operator_runbook_path": str(delivery_dir / "geoagent_operator_runbook_v1.md"),
        "next_stage": "closed_loop_5b_enterprise_api_service",
        "next_stage_zh": "下一步创建企业级 API 服务入口"
    }


def main() -> None:
    result = create_delivery_package()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Closed Loop 4D: remote-sensing evidence fusion.

Fuse rule-based verification and VLM verification results into a conservative
candidate-level evidence package.

This module accepts partial VLM results. Failed VLM samples are not discarded;
they are marked as requiring human review.
"""

from __future__ import annotations

import csv
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


def _bool(value: Any) -> bool:
    return value is True


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        return

    fieldnames = sorted({k for r in records for k in r.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {}
            for k, v in record.items():
                if isinstance(v, (dict, list)):
                    row[k] = json.dumps(v, ensure_ascii=False)
                else:
                    row[k] = v
            writer.writerow(row)


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = []
    lines.append("# Closed Loop 4D Remote Sensing Evidence Fusion Report")
    lines.append("")
    lines.append(f"- Fusion status: `{report['fusion_status']}`")
    lines.append(f"- Total sample count: {report['total_sample_count']}")
    lines.append(f"- Rule verified count: {report['rule_verified_count']}")
    lines.append(f"- VLM verified count: {report['vlm_verified_count']}")
    lines.append(f"- VLM failed count: {report['vlm_failed_count']}")
    lines.append(f"- Final favorable count: {report['final_favorable_count']}")
    lines.append(f"- Final uncertain count: {report['final_uncertain_count']}")
    lines.append(f"- Final unfavorable count: {report['final_unfavorable_count']}")
    lines.append(f"- Human review required count: {report['human_review_required_count']}")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- `favorable` means the remote-sensing evidence is relatively positive, but it is not an automatic approval.")
    lines.append("- `uncertain` means the candidate should be kept only with human review or more data.")
    lines.append("- `unfavorable` means visible evidence suggests risk, conflict, or poor suitability.")
    lines.append("- This report fuses rule-based Sentinel-2 checks and Qwen2.5-VL verification.")
    lines.append("")
    lines.append("## Candidate-level Results")
    lines.append("")
    lines.append("| sample_id | project_type | scope_kind | final_signal | decision | evidence_grade | reason |")
    lines.append("|---|---|---|---|---|---|---|")

    for item in report["records"]:
        reason = _safe_str(item.get("final_reason_zh")).replace("|", "，")
        lines.append(
            f"| {item.get('sample_id')} | {item.get('project_type')} | "
            f"{item.get('scope_kind')} | {item.get('final_remote_sensing_signal')} | "
            f"{item.get('final_decision')} | {item.get('evidence_grade')} | {reason} |"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fuse_one(
    sample_id: str,
    sample_meta: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    vlm: dict[str, Any] | None,
) -> dict[str, Any]:
    sample_meta = sample_meta or {}
    rule = rule or {}
    vlm = vlm or {}

    vlm_status = vlm.get("vlm_status", "missing")
    vlm_failed = vlm_status != "verified_by_vlm"

    water_any = _bool(rule.get("water_suspect")) or _bool(vlm.get("water_visible"))
    builtup_any = _bool(rule.get("builtup_or_hardened_surface_suspect")) or _bool(vlm.get("dense_builtup_visible"))
    vegetation_any = _bool(rule.get("vegetation_suspect")) or _bool(vlm.get("dense_vegetation_visible"))
    bare_open_any = _bool(rule.get("bare_or_open_land_suspect")) or _bool(vlm.get("bare_or_open_land_visible"))
    quality_issue_any = (
        _bool(rule.get("image_quality_issue"))
        or vlm.get("remote_sensing_quality") == "poor"
        or _bool(vlm.get("terrain_or_shadow_issue_visible"))
    )

    vlm_signal = vlm.get("site_screening_signal", "missing")
    rule_label = rule.get("verification_label", "missing")

    risk_reasons = []
    positive_reasons = []

    if water_any:
        risk_reasons.append("疑似存在水体")
    if builtup_any:
        risk_reasons.append("疑似存在建成区或硬化地表")
    if quality_issue_any:
        risk_reasons.append("影像质量、阴影或地形干扰明显")
    if vegetation_any:
        risk_reasons.append("植被覆盖较明显")
    if vlm_failed:
        risk_reasons.append("VLM 核验失败或缺失")

    if bare_open_any:
        positive_reasons.append("可见裸地或开阔地特征")
    if vlm_signal == "favorable":
        positive_reasons.append("VLM 给出相对正向信号")

    # Conservative decision policy.
    if water_any or builtup_any:
        final_signal = "unfavorable"
        final_decision = "not_recommended_or_strict_review"
        evidence_grade = "B" if not vlm_failed else "C"
    elif quality_issue_any or vlm_failed:
        final_signal = "uncertain"
        final_decision = "requires_human_review"
        evidence_grade = "C"
    elif vlm_signal == "favorable" and bare_open_any:
        final_signal = "favorable"
        final_decision = "potential_candidate_after_manual_review"
        evidence_grade = "A"
    elif bare_open_any:
        final_signal = "uncertain"
        final_decision = "potential_candidate_but_requires_review"
        evidence_grade = "B"
    else:
        final_signal = "uncertain"
        final_decision = "requires_human_review"
        evidence_grade = "B" if not vlm_failed else "C"

    # Do not auto-approve remote-sensing evidence.
    human_review_required = True

    if risk_reasons and positive_reasons:
        reason_zh = "；".join(risk_reasons + positive_reasons) + "，需人工复核。"
    elif risk_reasons:
        reason_zh = "；".join(risk_reasons) + "，需人工复核。"
    elif positive_reasons:
        reason_zh = "；".join(positive_reasons) + "，可作为候选但仍需人工复核。"
    else:
        reason_zh = "遥感证据不足以自动判断，需结合 GIS 约束和人工复核。"

    return {
        "sample_id": sample_id,
        "source_case_id": sample_meta.get("source_case_id") or rule.get("source_case_id"),
        "source_job_id": sample_meta.get("source_job_id") or rule.get("source_job_id") or vlm.get("source_job_id"),
        "project_type": sample_meta.get("project_type") or rule.get("project_type") or vlm.get("project_type"),
        "scope_kind": sample_meta.get("scope_kind") or rule.get("scope_kind") or vlm.get("scope_kind"),
        "source_final_candidate_path": sample_meta.get("source_final_candidate_path"),
        "candidate_count_in_source": sample_meta.get("candidate_count_in_source"),
        "area_km2": sample_meta.get("area_km2"),
        "centroid_wgs84": sample_meta.get("centroid_wgs84"),
        "bbox_wgs84": sample_meta.get("bbox_wgs84"),
        "rule_status": rule.get("verification_status", "missing"),
        "rule_label": rule_label,
        "rule_label_zh": rule.get("verification_label_zh"),
        "vlm_status": vlm_status,
        "vlm_signal": vlm_signal,
        "vlm_visible_land_cover_zh": vlm.get("visible_land_cover_zh"),
        "vlm_reason_zh": vlm.get("reason_zh"),
        "vlm_failure_reason": vlm.get("failure_reason"),
        "water_any": water_any,
        "builtup_any": builtup_any,
        "vegetation_any": vegetation_any,
        "bare_open_any": bare_open_any,
        "quality_issue_any": quality_issue_any,
        "final_remote_sensing_signal": final_signal,
        "final_decision": final_decision,
        "evidence_grade": evidence_grade,
        "human_review_required": human_review_required,
        "final_reason_zh": reason_zh,
    }


def run_remote_sensing_evidence_fusion() -> dict[str, Any]:
    package_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_candidate_sampling_package_v1.json"
    )
    rule_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_rule_based_verification_v1.json"
    )
    vlm_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_vlm_verification_v1.json"
    )

    package = read_json(package_path)
    rule_report = read_json(rule_path)
    vlm_report = read_json(vlm_path)

    sample_by_id = {
        r.get("sample_id"): r for r in package.get("sample_records", []) if r.get("sample_id")
    }
    rule_by_id = {
        r.get("sample_id"): r for r in rule_report.get("records", []) if r.get("sample_id")
    }
    vlm_by_id = {
        r.get("sample_id"): r for r in vlm_report.get("records", []) if r.get("sample_id")
    }

    sample_ids = sorted(set(sample_by_id) | set(rule_by_id) | set(vlm_by_id))
    if not sample_ids:
        raise RuntimeError("No samples found for remote-sensing evidence fusion.")

    records = []
    for sample_id in sample_ids:
        records.append(
            _fuse_one(
                sample_id=sample_id,
                sample_meta=sample_by_id.get(sample_id),
                rule=rule_by_id.get(sample_id),
                vlm=vlm_by_id.get(sample_id),
            )
        )

    total = len(records)
    favorable = sum(1 for r in records if r["final_remote_sensing_signal"] == "favorable")
    uncertain = sum(1 for r in records if r["final_remote_sensing_signal"] == "uncertain")
    unfavorable = sum(1 for r in records if r["final_remote_sensing_signal"] == "unfavorable")
    review_count = sum(1 for r in records if r["human_review_required"])

    vlm_verified = sum(1 for r in records if r["vlm_status"] == "verified_by_vlm")
    vlm_failed = total - vlm_verified
    rule_verified = sum(1 for r in records if r["rule_status"] == "verified_rule_based")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    report = {
        "evidence_fusion_id": "closed_loop_4d_remote_sensing_evidence_fusion_v1",
        "created_at_utc": now,
        "fusion_status": "completed",
        "fusion_status_zh": "闭环 4D 遥感证据融合完成",
        "fusion_policy": "conservative_rule_vlm_fusion_no_auto_approval",
        "fusion_policy_zh": "规则核验与 VLM 核验保守融合，不自动批准候选区",
        "source_candidate_package": str(package_path),
        "source_rule_report": str(rule_path),
        "source_vlm_report": str(vlm_path),
        "total_sample_count": total,
        "rule_verified_count": rule_verified,
        "vlm_verified_count": vlm_verified,
        "vlm_failed_count": vlm_failed,
        "final_favorable_count": favorable,
        "final_uncertain_count": uncertain,
        "final_unfavorable_count": unfavorable,
        "human_review_required_count": review_count,
        "do_not_auto_approve": True,
        "next_stage": "closed_loop_4e_remote_sensing_final_validation",
        "next_stage_zh": "下一步进行闭环 4 遥感核验总体验收",
        "records": records,
    }

    out_dir = RUNTIME_ROOT / "remote_sensing"
    json_path = out_dir / "closed_loop_4d_remote_sensing_evidence_fusion_v1.json"
    csv_path = out_dir / "closed_loop_4d_remote_sensing_evidence_fusion_records_v1.csv"
    md_path = out_dir / "closed_loop_4d_remote_sensing_evidence_fusion_report_v1.md"

    write_json(json_path, report)
    _write_csv(csv_path, records)
    _write_markdown(md_path, report)

    return {
        "closed_loop_4d_evidence_fusion_status": "completed",
        "closed_loop_4d_evidence_fusion_status_zh": "闭环 4D 遥感证据融合完成",
        "total_sample_count": total,
        "rule_verified_count": rule_verified,
        "vlm_verified_count": vlm_verified,
        "vlm_failed_count": vlm_failed,
        "final_favorable_count": favorable,
        "final_uncertain_count": uncertain,
        "final_unfavorable_count": unfavorable,
        "human_review_required_count": review_count,
        "json_report_path": str(json_path),
        "csv_records_path": str(csv_path),
        "markdown_report_path": str(md_path),
        "next_stage": report["next_stage"],
        "next_stage_zh": report["next_stage_zh"],
    }


def validate_remote_sensing_evidence_fusion() -> dict[str, Any]:
    report_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4d_remote_sensing_evidence_fusion_v1.json"
    )
    report = read_json(report_path)

    records = report.get("records", [])
    if not records:
        raise RuntimeError("Evidence fusion report has no records.")

    if report.get("total_sample_count") != len(records):
        raise RuntimeError("total_sample_count does not match records length.")

    for record in records:
        if record.get("final_remote_sensing_signal") not in {"favorable", "uncertain", "unfavorable"}:
            raise RuntimeError(f"Invalid final signal: {record.get('sample_id')}")
        if record.get("final_decision") is None:
            raise RuntimeError(f"Missing final decision: {record.get('sample_id')}")
        if record.get("final_reason_zh") is None:
            raise RuntimeError(f"Missing final reason: {record.get('sample_id')}")

    validation = {
        "closed_loop_4d_evidence_fusion_validation_status": "passed",
        "closed_loop_4d_status_zh": "闭环 4D 遥感证据融合验证通过",
        "source_report_path": str(report_path),
        "total_sample_count": report.get("total_sample_count"),
        "rule_verified_count": report.get("rule_verified_count"),
        "vlm_verified_count": report.get("vlm_verified_count"),
        "vlm_failed_count": report.get("vlm_failed_count"),
        "final_favorable_count": report.get("final_favorable_count"),
        "final_uncertain_count": report.get("final_uncertain_count"),
        "final_unfavorable_count": report.get("final_unfavorable_count"),
        "human_review_required_count": report.get("human_review_required_count"),
        "next_stage": report.get("next_stage"),
        "next_stage_zh": report.get("next_stage_zh"),
    }

    validation_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4d_remote_sensing_evidence_fusion_validation_v1.json"
    )
    write_json(validation_path, validation)

    result = dict(validation)
    result["validation_report_path"] = str(validation_path)
    return result

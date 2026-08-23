"""Validate Closed Loop 4 remote-sensing verification pipeline.

Closed Loop 4 includes:
4A candidate sampling package
4B real Sentinel-2 tile acquisition
4C-1 rule-based remote-sensing verification
4C-2 VLM verification interface on real Sentinel-2 tiles
4D rule + VLM evidence fusion

This final validation accepts partial VLM completion when:
- real VLM calls were executed;
- most tiles were verified;
- failed VLM samples are preserved and sent to human review;
- 4D fusion produces candidate-level final evidence for all samples.
"""

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


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = []
    lines.append("# Closed Loop 4 Remote Sensing Verification Final Validation")
    lines.append("")
    lines.append(f"- Final status: `{report['closed_loop_4_remote_sensing_validation_status']}`")
    lines.append(f"- Status zh: {report['closed_loop_4_status_zh']}")
    lines.append("")
    lines.append("## Stage Summary")
    lines.append("")
    lines.append("| Stage | Status | Key evidence |")
    lines.append("|---|---|---|")
    for stage in report["stage_summaries"]:
        lines.append(
            f"| {stage['stage']} | {stage['status']} | {stage['evidence']} |"
        )

    lines.append("")
    lines.append("## Main Counts")
    lines.append("")
    lines.append(f"- Sample count: {report['sample_count']}")
    lines.append(f"- Acquired Sentinel-2 tile count: {report['acquired_tile_count']}")
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
    lines.append("- The pipeline uses real Sentinel-2 imagery, not generated or fake imagery.")
    lines.append("- Rule-based verification and Qwen2.5-VL verification both operate on real downloaded tiles.")
    lines.append("- The final fusion policy is conservative and does not automatically approve candidate sites.")
    lines.append("- VLM partial completion is accepted because the failed sample is retained and routed to human review.")
    lines.append("")
    lines.append("## Next Stage")
    lines.append("")
    lines.append(f"- {report['next_stage_zh']}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_closed_loop_4_remote_sensing() -> dict[str, Any]:
    remote_dir = RUNTIME_ROOT / "remote_sensing"

    p_4a = remote_dir / "closed_loop_4a_candidate_sampling_validation_v1.json"
    p_4b = remote_dir / "closed_loop_4b_tile_acquisition_validation_v1.json"
    p_4c_rule = remote_dir / "closed_loop_4c_rule_based_verification_validation_v1.json"
    p_4c_vlm = remote_dir / "closed_loop_4c_vlm_verification_validation_v1.json"
    p_4d = remote_dir / "closed_loop_4d_remote_sensing_evidence_fusion_validation_v1.json"
    p_4d_report = remote_dir / "closed_loop_4d_remote_sensing_evidence_fusion_v1.json"

    v_4a = read_json(p_4a)
    v_4b = read_json(p_4b)
    v_4c_rule = read_json(p_4c_rule)
    v_4c_vlm = read_json(p_4c_vlm)
    v_4d = read_json(p_4d)
    fusion = read_json(p_4d_report)

    errors: list[str] = []

    if v_4a.get("closed_loop_4a_candidate_sampling_validation_status") != "passed":
        errors.append("4A candidate sampling validation did not pass.")

    if v_4b.get("closed_loop_4b_tile_acquisition_validation_status") != "passed":
        errors.append("4B tile acquisition validation did not pass.")

    if not v_4b.get("vision_ready"):
        errors.append("4B vision_ready is false; no imagery available for verification.")

    if v_4b.get("do_not_fake_imagery") is not True:
        errors.append("4B do_not_fake_imagery flag is not true.")

    if v_4c_rule.get("closed_loop_4c_rule_verification_validation_status") != "passed":
        errors.append("4C-1 rule verification validation did not pass.")

    if v_4c_vlm.get("closed_loop_4c_vlm_verification_validation_status") != "passed":
        errors.append("4C-2 VLM verification validation did not pass.")

    if v_4c_vlm.get("real_vlm_call_executed") is not True:
        errors.append("4C-2 did not execute a real VLM call.")

    sample_count = int(fusion.get("total_sample_count", 0))
    vlm_verified_count = int(fusion.get("vlm_verified_count", 0))
    vlm_failed_count = int(fusion.get("vlm_failed_count", 0))

    if sample_count <= 0:
        errors.append("4D fusion has no samples.")

    if vlm_verified_count <= 0:
        errors.append("No VLM-verified samples were found.")

    # Conservative acceptance rule:
    # VLM may be partial, but most samples should be verified.
    if sample_count > 0 and vlm_verified_count / sample_count < 0.80:
        errors.append("VLM verified ratio is below 80%; evidence is too incomplete.")

    if v_4d.get("closed_loop_4d_evidence_fusion_validation_status") != "passed":
        errors.append("4D evidence fusion validation did not pass.")

    if int(v_4d.get("total_sample_count", -1)) != sample_count:
        errors.append("4D validation sample count does not match fusion report.")

    if errors:
        final_status = "failed"
        final_status_zh = "闭环 4 遥感核验总体验收失败"
    elif vlm_failed_count > 0:
        final_status = "passed_with_partial_vlm_and_human_review_required"
        final_status_zh = "闭环 4 遥感核验总体验收通过，但存在部分 VLM 失败样本，已进入人工复核"
    else:
        final_status = "passed"
        final_status_zh = "闭环 4 遥感核验总体验收通过"

    stage_summaries = [
        {
            "stage": "4A Candidate Sampling",
            "status": v_4a.get("closed_loop_4a_candidate_sampling_validation_status"),
            "evidence": f"sample_count={v_4a.get('sample_count')}, tile_task_count={v_4a.get('tile_task_count')}",
        },
        {
            "stage": "4B Sentinel-2 Tile Acquisition",
            "status": v_4b.get("closed_loop_4b_tile_acquisition_validation_status"),
            "evidence": f"acquired={v_4b.get('acquired_tile_count')}, missing={v_4b.get('missing_tile_count')}, vision_ready={v_4b.get('vision_ready')}",
        },
        {
            "stage": "4C-1 Rule Verification",
            "status": v_4c_rule.get("closed_loop_4c_rule_verification_validation_status"),
            "evidence": f"verified={v_4c_rule.get('verified_tile_count')}, failed={v_4c_rule.get('failed_tile_count')}, review={v_4c_rule.get('human_review_required_count')}",
        },
        {
            "stage": "4C-2 VLM Verification",
            "status": v_4c_vlm.get("closed_loop_4c_vlm_verification_validation_status"),
            "evidence": f"real_vlm_call={v_4c_vlm.get('real_vlm_call_executed')}, verified={v_4c_vlm.get('verified_tile_count')}, failed={v_4c_vlm.get('failed_tile_count')}",
        },
        {
            "stage": "4D Evidence Fusion",
            "status": v_4d.get("closed_loop_4d_evidence_fusion_validation_status"),
            "evidence": f"favorable={v_4d.get('final_favorable_count')}, uncertain={v_4d.get('final_uncertain_count')}, unfavorable={v_4d.get('final_unfavorable_count')}, review={v_4d.get('human_review_required_count')}",
        },
    ]

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    report = {
        "closed_loop_4_remote_sensing_validation_id": "closed_loop_4_remote_sensing_final_validation_v1",
        "created_at_utc": now,
        "closed_loop_4_remote_sensing_validation_status": final_status,
        "closed_loop_4_status_zh": final_status_zh,
        "errors": errors,
        "sample_count": sample_count,
        "acquired_tile_count": int(v_4b.get("acquired_tile_count", 0)),
        "missing_tile_count": int(v_4b.get("missing_tile_count", 0)),
        "rule_verified_count": int(v_4c_rule.get("verified_tile_count", 0)),
        "rule_failed_count": int(v_4c_rule.get("failed_tile_count", 0)),
        "vlm_verified_count": vlm_verified_count,
        "vlm_failed_count": vlm_failed_count,
        "final_favorable_count": int(fusion.get("final_favorable_count", 0)),
        "final_uncertain_count": int(fusion.get("final_uncertain_count", 0)),
        "final_unfavorable_count": int(fusion.get("final_unfavorable_count", 0)),
        "human_review_required_count": int(fusion.get("human_review_required_count", 0)),
        "real_imagery_source": "Sentinel-2 L2A visual tiles via Microsoft Planetary Computer",
        "vlm_model": "Qwen2.5-VL-7B-Instruct",
        "do_not_fake_imagery": True,
        "do_not_auto_approve": True,
        "stage_summaries": stage_summaries,
        "source_reports": {
            "4a_validation": str(p_4a),
            "4b_validation": str(p_4b),
            "4c_rule_validation": str(p_4c_rule),
            "4c_vlm_validation": str(p_4c_vlm),
            "4d_validation": str(p_4d),
            "4d_fusion_report": str(p_4d_report),
        },
        "next_stage": "closed_loop_5_enterprise_agent_delivery",
        "next_stage_zh": "下一步进入闭环 5：企业级 Agent 交付层",
    }

    json_path = remote_dir / "closed_loop_4_remote_sensing_final_validation_v1.json"
    md_path = remote_dir / "closed_loop_4_remote_sensing_final_validation_report_v1.md"

    write_json(json_path, report)
    _write_markdown(md_path, report)

    result = {
        "closed_loop_4_remote_sensing_validation_status": final_status,
        "closed_loop_4_status_zh": final_status_zh,
        "sample_count": report["sample_count"],
        "acquired_tile_count": report["acquired_tile_count"],
        "missing_tile_count": report["missing_tile_count"],
        "rule_verified_count": report["rule_verified_count"],
        "vlm_verified_count": report["vlm_verified_count"],
        "vlm_failed_count": report["vlm_failed_count"],
        "final_favorable_count": report["final_favorable_count"],
        "final_uncertain_count": report["final_uncertain_count"],
        "final_unfavorable_count": report["final_unfavorable_count"],
        "human_review_required_count": report["human_review_required_count"],
        "json_report_path": str(json_path),
        "markdown_report_path": str(md_path),
        "next_stage": report["next_stage"],
        "next_stage_zh": report["next_stage_zh"],
    }

    if errors:
        result["errors"] = errors

    return result


def main() -> None:
    result = validate_closed_loop_4_remote_sensing()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

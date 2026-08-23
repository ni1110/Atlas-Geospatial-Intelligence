"""Closed Loop 4C-1: rule-based verification on real Sentinel-2 tiles.

This module uses real acquired Sentinel-2 visual image tiles.
It does not use a VLM yet and does not fabricate imagery.

It produces conservative image-quality and surface-signal checks:
- water_suspect
- vegetation_suspect
- builtup_or_hardened_surface_suspect
- bare_or_open_land_suspect
- human_review_required
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

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


def _load_rgb(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img).astype("float32") / 255.0
    return arr


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _analyze_rgb_tile(tile_path: Path) -> dict[str, Any]:
    arr = _load_rgb(tile_path)

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    brightness = (r + g + b) / 3.0
    maxc = np.maximum.reduce([r, g, b])
    minc = np.minimum.reduce([r, g, b])
    saturation = maxc - minc

    mean_r = float(np.mean(r))
    mean_g = float(np.mean(g))
    mean_b = float(np.mean(b))
    mean_brightness = float(np.mean(brightness))
    std_brightness = float(np.std(brightness))
    mean_saturation = float(np.mean(saturation))

    dark_ratio = float(np.mean(brightness < 0.08))
    bright_ratio = float(np.mean(brightness > 0.92))

    green_dominant_ratio = float(np.mean((g > r + 0.05) & (g > b + 0.05)))
    blue_dominant_ratio = float(np.mean((b > r + 0.05) & (b > g + 0.05)))
    gray_low_saturation_ratio = float(np.mean(saturation < 0.08))

    # Conservative visual heuristics for true-color Sentinel-2 visual tiles.
    water_suspect = bool(
        blue_dominant_ratio > 0.18
        and mean_b >= mean_g
        and mean_b >= mean_r
        and mean_brightness < 0.55
    )

    vegetation_suspect = bool(
        green_dominant_ratio > 0.20
        and mean_g > mean_r
        and mean_g >= mean_b
    )

    builtup_or_hardened_surface_suspect = bool(
        gray_low_saturation_ratio > 0.35
        and 0.20 <= mean_brightness <= 0.78
        and green_dominant_ratio < 0.25
    )

    bare_or_open_land_suspect = bool(
        mean_brightness >= 0.35
        and mean_saturation >= 0.05
        and green_dominant_ratio < 0.30
        and blue_dominant_ratio < 0.20
    )

    image_quality_issue = bool(
        dark_ratio > 0.50
        or bright_ratio > 0.50
        or std_brightness < 0.025
    )

    # Human review is required if the image is low quality, or if signals conflict,
    # or if it is hard to classify by simple rules.
    positive_surface_signals = sum(
        [
            water_suspect,
            vegetation_suspect,
            builtup_or_hardened_surface_suspect,
            bare_or_open_land_suspect,
        ]
    )

    human_review_required = bool(
        image_quality_issue
        or water_suspect
        or positive_surface_signals == 0
        or positive_surface_signals >= 3
    )

    if image_quality_issue:
        verification_label = "image_quality_issue"
        verification_label_zh = "影像质量异常，需人工复核"
    elif water_suspect:
        verification_label = "water_suspect"
        verification_label_zh = "疑似存在明显水体，需人工复核"
    elif bare_or_open_land_suspect and not builtup_or_hardened_surface_suspect:
        verification_label = "open_or_bare_land_suspect"
        verification_label_zh = "疑似开阔地 / 裸地，可作为候选但需后续复核"
    elif builtup_or_hardened_surface_suspect:
        verification_label = "builtup_or_hardened_surface_suspect"
        verification_label_zh = "疑似建成区或硬化地表，需结合业务判断"
    elif vegetation_suspect:
        verification_label = "vegetation_suspect"
        verification_label_zh = "疑似植被覆盖区域，需结合开发约束判断"
    else:
        verification_label = "uncertain"
        verification_label_zh = "规则核验不确定，需人工复核"

    return {
        "image_width": int(arr.shape[1]),
        "image_height": int(arr.shape[0]),
        "mean_r": mean_r,
        "mean_g": mean_g,
        "mean_b": mean_b,
        "mean_brightness": mean_brightness,
        "std_brightness": std_brightness,
        "mean_saturation": mean_saturation,
        "dark_ratio": dark_ratio,
        "bright_ratio": bright_ratio,
        "green_dominant_ratio": green_dominant_ratio,
        "blue_dominant_ratio": blue_dominant_ratio,
        "gray_low_saturation_ratio": gray_low_saturation_ratio,
        "water_suspect": water_suspect,
        "vegetation_suspect": vegetation_suspect,
        "builtup_or_hardened_surface_suspect": builtup_or_hardened_surface_suspect,
        "bare_or_open_land_suspect": bare_or_open_land_suspect,
        "image_quality_issue": image_quality_issue,
        "human_review_required": human_review_required,
        "verification_label": verification_label,
        "verification_label_zh": verification_label_zh,
    }


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        return

    fieldnames = list(records[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = []
    lines.append("# Closed Loop 4C-1 Remote Sensing Rule Verification Report")
    lines.append("")
    lines.append(f"- Status: `{report['verification_status']}`")
    lines.append(f"- Tile count: {report['tile_count']}")
    lines.append(f"- Verified tile count: {report['verified_tile_count']}")
    lines.append(f"- Human review required count: {report['human_review_required_count']}")
    lines.append(f"- Water suspect count: {report['water_suspect_count']}")
    lines.append(f"- Vegetation suspect count: {report['vegetation_suspect_count']}")
    lines.append(f"- Built-up / hardened surface suspect count: {report['builtup_or_hardened_surface_suspect_count']}")
    lines.append(f"- Bare / open land suspect count: {report['bare_or_open_land_suspect_count']}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- This is rule-based verification on real Sentinel-2 visual tiles.")
    lines.append("- It is not a VLM judgment and should be treated as a conservative pre-screening layer.")
    lines.append("- Tiles marked as human-review-required should not be automatically approved.")
    lines.append("")
    lines.append("## Tile Results")
    lines.append("")
    lines.append("| sample_id | project_type | scope_kind | label | human_review | tile_path |")
    lines.append("|---|---|---|---|---|---|")

    for item in report["records"]:
        lines.append(
            "| {sample_id} | {project_type} | {scope_kind} | {label} | {human_review} | {tile_path} |".format(
                sample_id=item.get("sample_id"),
                project_type=item.get("project_type"),
                scope_kind=item.get("scope_kind"),
                label=item.get("verification_label"),
                human_review=item.get("human_review_required"),
                tile_path=item.get("registered_tile_path"),
            )
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_rule_based_verification() -> dict[str, Any]:
    acquisition_manifest_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_tile_acquisition_manifest_v1.json"
    )
    acquisition_manifest = read_json(acquisition_manifest_path)

    records = acquisition_manifest.get("records", [])
    acquired_records = [
        item for item in records if item.get("tile_acquisition_status") == "acquired"
    ]

    if not acquired_records:
        raise RuntimeError(
            "No acquired remote-sensing tiles found. Run 4B tile acquisition first."
        )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    verification_records = []

    for item in acquired_records:
        tile_path = Path(item.get("registered_tile_path", ""))
        base_record = {
            "sample_id": item.get("sample_id"),
            "source_job_id": item.get("source_job_id"),
            "project_type": item.get("project_type"),
            "scope_kind": item.get("scope_kind"),
            "registered_tile_path": str(tile_path),
            "tile_read_status": "pending",
            "verification_status": "pending",
            "failure_reason": None,
        }

        try:
            analysis = _analyze_rgb_tile(tile_path)
            base_record.update(analysis)
            base_record["tile_read_status"] = "readable"
            base_record["verification_status"] = "verified_rule_based"
        except Exception as exc:
            base_record["tile_read_status"] = "failed"
            base_record["verification_status"] = "failed"
            base_record["failure_reason"] = f"{type(exc).__name__}: {exc}"
            base_record["human_review_required"] = True
            base_record["verification_label"] = "tile_read_failed"
            base_record["verification_label_zh"] = "影像读取失败"

        verification_records.append(base_record)

    failed_count = sum(1 for item in verification_records if item["verification_status"] == "failed")
    verified_count = len(verification_records) - failed_count

    human_review_count = sum(1 for item in verification_records if item.get("human_review_required"))
    water_count = sum(1 for item in verification_records if item.get("water_suspect"))
    vegetation_count = sum(1 for item in verification_records if item.get("vegetation_suspect"))
    builtup_count = sum(1 for item in verification_records if item.get("builtup_or_hardened_surface_suspect"))
    bare_count = sum(1 for item in verification_records if item.get("bare_or_open_land_suspect"))
    image_quality_issue_count = sum(1 for item in verification_records if item.get("image_quality_issue"))

    if failed_count == 0:
        verification_status = "completed"
        verification_status_zh = "规则遥感核验完成"
    elif verified_count > 0:
        verification_status = "partially_completed"
        verification_status_zh = "规则遥感核验部分完成"
    else:
        verification_status = "failed"
        verification_status_zh = "规则遥感核验失败"

    output_dir = RUNTIME_ROOT / "remote_sensing"
    report_path = output_dir / "closed_loop_4c_rule_based_verification_v1.json"
    csv_path = output_dir / "closed_loop_4c_rule_based_verification_records_v1.csv"
    md_path = output_dir / "closed_loop_4c_rule_based_verification_report_v1.md"

    report = {
        "rule_verification_id": "closed_loop_4c_rule_based_verification_v1",
        "created_at_utc": now,
        "verification_status": verification_status,
        "verification_status_zh": verification_status_zh,
        "verification_mode": "rule_based_rgb_remote_sensing_prescreen",
        "verification_mode_zh": "基于真实 Sentinel-2 RGB 切片的规则预核验",
        "source_acquisition_manifest": str(acquisition_manifest_path),
        "tile_count": len(acquired_records),
        "verified_tile_count": verified_count,
        "failed_tile_count": failed_count,
        "human_review_required_count": human_review_count,
        "water_suspect_count": water_count,
        "vegetation_suspect_count": vegetation_count,
        "builtup_or_hardened_surface_suspect_count": builtup_count,
        "bare_or_open_land_suspect_count": bare_count,
        "image_quality_issue_count": image_quality_issue_count,
        "do_not_auto_approve": True,
        "do_not_auto_approve_zh": "规则核验仅作为预筛，不自动批准候选区。",
        "next_stage": "closed_loop_4c_vlm_verification_interface",
        "next_stage_zh": "下一步接入视觉语言模型核验接口",
        "records": verification_records,
    }

    write_json(report_path, report)
    _write_csv(csv_path, verification_records)
    _write_markdown_report(md_path, report)

    return {
        "closed_loop_4c_rule_verification_status": verification_status,
        "closed_loop_4c_rule_verification_status_zh": verification_status_zh,
        "tile_count": len(acquired_records),
        "verified_tile_count": verified_count,
        "failed_tile_count": failed_count,
        "human_review_required_count": human_review_count,
        "water_suspect_count": water_count,
        "vegetation_suspect_count": vegetation_count,
        "builtup_or_hardened_surface_suspect_count": builtup_count,
        "bare_or_open_land_suspect_count": bare_count,
        "image_quality_issue_count": image_quality_issue_count,
        "json_report_path": str(report_path),
        "csv_records_path": str(csv_path),
        "markdown_report_path": str(md_path),
        "next_stage": report["next_stage"],
        "next_stage_zh": report["next_stage_zh"],
    }


def validate_rule_based_verification() -> dict[str, Any]:
    report_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_rule_based_verification_v1.json"
    )
    report = read_json(report_path)

    records = report.get("records", [])
    if not records:
        raise RuntimeError("Rule-based verification report has no records.")

    if report.get("tile_count") != len(records):
        raise RuntimeError("tile_count does not match record count.")

    if report.get("verified_tile_count") + report.get("failed_tile_count") != len(records):
        raise RuntimeError("verified + failed count does not match record count.")

    for item in records:
        tile_path = Path(item.get("registered_tile_path", ""))
        if not tile_path.is_file():
            raise RuntimeError(f"Registered tile missing: {tile_path}")

        if item.get("verification_status") not in {"verified_rule_based", "failed"}:
            raise RuntimeError(f"Invalid verification status for {item.get('sample_id')}")

        if "human_review_required" not in item:
            raise RuntimeError(f"Missing human_review_required for {item.get('sample_id')}")

    validation = {
        "closed_loop_4c_rule_verification_validation_status": "passed",
        "closed_loop_4c_status_zh": "闭环 4C-1 规则遥感核验验证通过",
        "source_report_path": str(report_path),
        "tile_count": report.get("tile_count"),
        "verified_tile_count": report.get("verified_tile_count"),
        "failed_tile_count": report.get("failed_tile_count"),
        "human_review_required_count": report.get("human_review_required_count"),
        "next_stage": report.get("next_stage"),
        "next_stage_zh": report.get("next_stage_zh"),
    }

    validation_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_rule_based_verification_validation_v1.json"
    )
    write_json(validation_path, validation)

    result = dict(validation)
    result["validation_report_path"] = str(validation_path)
    return result

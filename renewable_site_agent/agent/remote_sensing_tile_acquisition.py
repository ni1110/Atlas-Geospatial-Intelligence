"""Closed Loop 4B: remote-sensing tile acquisition interface.

This module does not fake satellite imagery. It supports:
1. local tile registration if imagery files already exist;
2. explicit missing-imagery records if no tile is available;
3. provider interface records for future Sentinel / map tile / enterprise imagery integration.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


SUPPORTED_IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _find_local_tile(local_tile_dir: Path, sample_id: str) -> Path | None:
    if not local_tile_dir.is_dir():
        return None

    candidates = []
    for suffix in SUPPORTED_IMAGE_SUFFIXES:
        candidates.extend(local_tile_dir.glob(f"{sample_id}{suffix}"))
        candidates.extend(local_tile_dir.glob(f"{sample_id}_z*{suffix}"))
        candidates.extend(local_tile_dir.glob(f"*{sample_id}*{suffix}"))

    candidates = sorted(set(candidates), key=lambda p: len(str(p)))
    if candidates:
        return candidates[0]

    return None


def create_tile_acquisition_manifest(
    local_tile_dir: str | None = None,
    provider_name: str = "local_file",
) -> dict[str, Any]:
    tile_manifest_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_tile_request_manifest_v1.json"
    )
    tile_manifest = read_json(tile_manifest_path)

    if local_tile_dir is None:
        local_tile_root = RUNTIME_ROOT / "remote_sensing" / "tile_inputs"
    else:
        local_tile_root = Path(local_tile_dir)

    acquired_dir = RUNTIME_ROOT / "remote_sensing" / "acquired_tiles"
    acquired_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    acquisition_records = []

    for task in tile_manifest.get("tile_tasks", []):
        sample_id = task["sample_id"]
        src_tile = _find_local_tile(local_tile_root, sample_id)

        if src_tile is not None:
            dst_tile = acquired_dir / f"{sample_id}{src_tile.suffix.lower()}"
            shutil.copy2(src_tile, dst_tile)

            record = {
                "sample_id": sample_id,
                "source_job_id": task.get("source_job_id"),
                "project_type": task.get("project_type"),
                "scope_kind": task.get("scope_kind"),
                "centroid_wgs84": task.get("centroid_wgs84"),
                "bbox_wgs84": task.get("bbox_wgs84"),
                "tile_acquisition_status": "acquired",
                "tile_acquisition_status_zh": "已接入本地遥感切片",
                "provider_name": provider_name,
                "source_tile_path": str(src_tile),
                "registered_tile_path": str(dst_tile),
                "vision_ready": True,
            }
        else:
            record = {
                "sample_id": sample_id,
                "source_job_id": task.get("source_job_id"),
                "project_type": task.get("project_type"),
                "scope_kind": task.get("scope_kind"),
                "centroid_wgs84": task.get("centroid_wgs84"),
                "bbox_wgs84": task.get("bbox_wgs84"),
                "tile_acquisition_status": "missing",
                "tile_acquisition_status_zh": "未找到本地遥感切片",
                "provider_name": provider_name,
                "expected_local_tile_dir": str(local_tile_root),
                "expected_filename_patterns": [
                    f"{sample_id}.png",
                    f"{sample_id}.jpg",
                    f"{sample_id}_z14.png",
                    f"*{sample_id}*.png",
                ],
                "registered_tile_path": None,
                "vision_ready": False,
            }

        acquisition_records.append(record)

    acquired_count = sum(
        1 for item in acquisition_records if item["tile_acquisition_status"] == "acquired"
    )
    missing_count = len(acquisition_records) - acquired_count

    if acquired_count == len(acquisition_records):
        acquisition_status = "completed"
        acquisition_status_zh = "遥感切片全部接入完成"
    elif acquired_count > 0:
        acquisition_status = "partially_completed"
        acquisition_status_zh = "部分遥感切片已接入，仍有缺失"
    else:
        acquisition_status = "blocked_missing_imagery"
        acquisition_status_zh = "缺少遥感影像输入，不能进入视觉核验"

    acquisition_manifest = {
        "tile_acquisition_manifest_id": "closed_loop_4b_tile_acquisition_manifest_v1",
        "created_at_utc": now,
        "acquisition_status": acquisition_status,
        "acquisition_status_zh": acquisition_status_zh,
        "provider_name": provider_name,
        "provider_mode": "local_file_registration",
        "provider_mode_zh": "本地影像切片登记模式",
        "source_tile_request_manifest": str(tile_manifest_path),
        "local_tile_root": str(local_tile_root),
        "acquired_tile_dir": str(acquired_dir),
        "tile_task_count": len(acquisition_records),
        "acquired_tile_count": acquired_count,
        "missing_tile_count": missing_count,
        "do_not_fake_imagery": True,
        "do_not_fake_imagery_zh": "未找到影像时只记录缺失，不生成伪影像冒充遥感数据。",
        "records": acquisition_records,
        "next_stage": "closed_loop_4c_multimodal_verification"
        if acquired_count > 0
        else "provide_remote_sensing_tiles_or_configure_imagery_provider",
        "next_stage_zh": "下一步进入多模态核验"
        if acquired_count > 0
        else "请提供遥感切片或配置影像服务后再进入多模态核验",
    }

    output_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_tile_acquisition_manifest_v1.json"
    )
    write_json(output_path, acquisition_manifest)

    return {
        "tile_acquisition_status": acquisition_status,
        "tile_acquisition_status_zh": acquisition_status_zh,
        "provider_name": provider_name,
        "tile_task_count": len(acquisition_records),
        "acquired_tile_count": acquired_count,
        "missing_tile_count": missing_count,
        "tile_acquisition_manifest_path": str(output_path),
        "local_tile_root": str(local_tile_root),
        "next_stage": "closed_loop_4c_multimodal_verification"
        if acquired_count > 0
        else "provide_remote_sensing_tiles_or_configure_imagery_provider",
        "next_stage_zh": "下一步进入多模态核验"
        if acquired_count > 0
        else "请提供遥感切片或配置影像服务后再进入多模态核验",
    }


def validate_tile_acquisition_manifest(allow_missing: bool = True) -> dict[str, Any]:
    manifest_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_tile_acquisition_manifest_v1.json"
    )
    manifest = read_json(manifest_path)

    records = manifest.get("records", [])
    if not records:
        raise RuntimeError("Tile acquisition manifest has no records.")

    acquired = [
        record for record in records if record.get("tile_acquisition_status") == "acquired"
    ]
    missing = [
        record for record in records if record.get("tile_acquisition_status") == "missing"
    ]

    for record in acquired:
        tile_path = Path(record.get("registered_tile_path", ""))
        if not tile_path.is_file():
            raise RuntimeError(f"Registered tile is missing: {tile_path}")

    if missing and not allow_missing:
        raise RuntimeError(
            f"Missing {len(missing)} imagery tiles and allow_missing=False."
        )

    validation_status = "passed_with_missing_imagery" if missing else "passed"

    validation = {
        "closed_loop_4b_tile_acquisition_validation_status": validation_status,
        "closed_loop_4b_status_zh": "闭环 4B 遥感切片接入接口验证通过",
        "tile_acquisition_manifest_path": str(manifest_path),
        "acquisition_status": manifest.get("acquisition_status"),
        "tile_task_count": len(records),
        "acquired_tile_count": len(acquired),
        "missing_tile_count": len(missing),
        "allow_missing": allow_missing,
        "vision_ready": len(acquired) > 0,
        "do_not_fake_imagery": manifest.get("do_not_fake_imagery"),
        "next_stage": manifest.get("next_stage"),
        "next_stage_zh": manifest.get("next_stage_zh"),
    }

    validation_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_tile_acquisition_validation_v1.json"
    )
    write_json(validation_path, validation)

    result = dict(validation)
    result["validation_report_path"] = str(validation_path)
    return result

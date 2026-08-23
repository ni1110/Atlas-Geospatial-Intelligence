"""Closed Loop 4C-2: VLM verification interface for real remote-sensing tiles.

This module calls a real OpenAI-compatible VLM endpoint when configured.
If no VLM provider is configured, it writes a blocked report and does not fake results.
"""

from __future__ import annotations

import base64
import csv
import json
import mimetypes
import os
import re
import urllib.request
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


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        return

    fieldnames = sorted({k for record in records for k in record.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _load_provider_config() -> dict[str, Any]:
    base_url = (
        os.getenv("GEOAGENT_VLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ""
    ).rstrip("/")

    api_key = (
        os.getenv("GEOAGENT_VLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )

    model = (
        os.getenv("GEOAGENT_VLM_MODEL")
        or os.getenv("OPENAI_MODEL")
        or ""
    )

    provider_ready = bool(base_url and model)

    return {
        "provider": "openai_compatible",
        "provider_ready": provider_ready,
        "base_url": base_url,
        "api_key_configured": bool(api_key),
        "model": model,
        "config_source": "environment_variables",
        "required_env": [
            "GEOAGENT_VLM_BASE_URL",
            "GEOAGENT_VLM_MODEL",
            "GEOAGENT_VLM_API_KEY optional for local endpoints",
        ],
    }


def _image_to_data_url(image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if mime_type is None:
        mime_type = "image/png"

    encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("VLM response does not contain valid JSON.")


def _call_openai_compatible_vlm(
    base_url: str,
    api_key: str,
    model: str,
    image_path: Path,
    prompt: str,
    timeout_sec: int,
) -> dict[str, Any]:
    url = f"{base_url}/chat/completions"
    data_url = _image_to_data_url(image_path)

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 900,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a remote-sensing verification assistant for renewable-site screening. "
                    "Return only valid JSON. Do not invent observations that are not visible."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8")

    response = json.loads(raw)
    content = response["choices"][0]["message"]["content"]
    parsed = _extract_json(content)

    return {
        "raw_response": content,
        "parsed_response": parsed,
    }



def _first_present(data: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "是", "有", "visible", "存在"}:
            return True
        if v in {"false", "no", "否", "无", "not visible", "不存在"}:
            return False
    return default


def _to_float(value: Any, default: float = 0.5) -> float:
    try:
        v = float(value)
        return max(0.0, min(1.0, v))
    except Exception:
        return default


def _normalize_quality(value: Any) -> str:
    if not isinstance(value, str):
        return "medium"
    v = value.strip().lower()
    if v in {"good", "medium", "poor"}:
        return v
    if v in {"好", "较好", "清晰"}:
        return "good"
    if v in {"差", "较差", "模糊"}:
        return "poor"
    return "medium"


def _normalize_signal(value: Any) -> str:
    if not isinstance(value, str):
        return "uncertain"
    v = value.strip().lower()
    if v in {"favorable", "suitable", "适合", "较适合", "有利"}:
        return "favorable"
    if v in {"unfavorable", "unsuitable", "不适合", "不利"}:
        return "unfavorable"
    return "uncertain"



def _fallback_land_cover_zh(
    water_visible: bool,
    dense_builtup_visible: bool,
    dense_vegetation_visible: bool,
    bare_or_open_land_visible: bool,
    terrain_or_shadow_issue_visible: bool,
    quality: str,
) -> str:
    parts = []
    if water_visible:
        parts.append("可见疑似水体")
    if dense_builtup_visible:
        parts.append("可见疑似密集建成区或硬化地表")
    if dense_vegetation_visible:
        parts.append("可见较多植被覆盖")
    if bare_or_open_land_visible:
        parts.append("可见疑似裸地或开阔地")
    if terrain_or_shadow_issue_visible:
        parts.append("存在地形阴影或遮挡干扰")
    if quality == "poor":
        parts.append("影像质量较差")
    if not parts:
        parts.append("地表类型不够明确")
    return "；".join(parts) + "。"


def _fallback_reason_zh(
    signal: str,
    human_review_required: bool,
    water_visible: bool,
    dense_builtup_visible: bool,
    dense_vegetation_visible: bool,
    bare_or_open_land_visible: bool,
    terrain_or_shadow_issue_visible: bool,
    quality: str,
) -> str:
    if water_visible:
        return "候选区内疑似存在水体，不宜直接通过，需人工复核。"
    if dense_builtup_visible:
        return "候选区疑似存在密集建成区或硬化地表，需结合用地现状复核。"
    if terrain_or_shadow_issue_visible or quality == "poor":
        return "影像存在质量、阴影或地形干扰，模型判断不稳定，需人工复核。"
    if bare_or_open_land_visible and signal == "favorable":
        return "图像中可见开阔或裸地特征，具备候选潜力，但仍需人工核验。"
    if dense_vegetation_visible:
        return "候选区植被覆盖较明显，开发适宜性需要结合生态和用地约束判断。"
    if human_review_required:
        return "模型判断存在不确定性，建议进入人工复核。"
    return "图像未显示明显冲突因素，但仍需结合 GIS 约束和现场数据确认。"



def _normalize_vlm_response(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize unstable VLM JSON keys into the project contract."""
    visible_land_cover_zh = _first_present(
        parsed,
        [
            "visible_land_cover_zh",
            "visible_land_cover_zzh",
            "visible_land_cover",
            "land_cover_zh",
            "land_cover",
            "surface_description_zh",
            "地表描述",
        ],
        "未提供明确地表描述",
    )

    reason_zh = _first_present(
        parsed,
        [
            "reason_zh",
            "reason_zzh",
            "reason",
            "explanation_zh",
            "explanation",
            "rationale_zh",
            "rationale",
            "判断理由",
        ],
        "模型未提供明确理由，需人工复核。",
    )

    water_visible = _to_bool(
        _first_present(parsed, ["water_visible", "water", "水体可见", "visible_water"]),
        False,
    )
    dense_builtup_visible = _to_bool(
        _first_present(parsed, ["dense_builtup_visible", "builtup_visible", "building_visible", "dense_building_visible", "密集建成区可见"]),
        False,
    )
    dense_vegetation_visible = _to_bool(
        _first_present(parsed, ["dense_vegetation_visible", "vegetation_visible", "dense_green_visible", "密集植被可见"]),
        False,
    )
    bare_or_open_land_visible = _to_bool(
        _first_present(parsed, ["bare_or_open_land_visible", "open_land_visible", "bare_land_visible", "裸地或开阔地可见"]),
        False,
    )
    terrain_or_shadow_issue_visible = _to_bool(
        _first_present(parsed, ["terrain_or_shadow_issue_visible", "terrain_issue_visible", "shadow_issue_visible", "地形或阴影问题可见"]),
        False,
    )

    quality = _normalize_quality(
        _first_present(parsed, ["remote_sensing_quality", "image_quality", "quality", "影像质量"], "medium")
    )

    signal = _normalize_signal(
        _first_present(parsed, ["site_screening_signal", "screening_signal", "site_signal", "选址信号"], "uncertain")
    )

    human_review_required = _to_bool(
        _first_present(parsed, ["human_review_required", "need_human_review", "manual_review_required", "需要人工复核"]),
        True,
    )

    confidence = _to_float(
        _first_present(parsed, ["confidence", "score", "置信度"], 0.5),
        0.5,
    )

    # Conservative correction: obvious risk signals require human review.
    if water_visible or dense_builtup_visible or terrain_or_shadow_issue_visible or quality == "poor":
        human_review_required = True
        if signal == "favorable":
            signal = "uncertain"

    if visible_land_cover_zh in {None, "", "未提供明确地表描述"}:
        visible_land_cover_zh = _fallback_land_cover_zh(
            water_visible=water_visible,
            dense_builtup_visible=dense_builtup_visible,
            dense_vegetation_visible=dense_vegetation_visible,
            bare_or_open_land_visible=bare_or_open_land_visible,
            terrain_or_shadow_issue_visible=terrain_or_shadow_issue_visible,
            quality=quality,
        )

    if reason_zh in {None, "", "模型未提供明确理由，需人工复核。"}:
        reason_zh = _fallback_reason_zh(
            signal=signal,
            human_review_required=human_review_required,
            water_visible=water_visible,
            dense_builtup_visible=dense_builtup_visible,
            dense_vegetation_visible=dense_vegetation_visible,
            bare_or_open_land_visible=bare_or_open_land_visible,
            terrain_or_shadow_issue_visible=terrain_or_shadow_issue_visible,
            quality=quality,
        )

    return {
        "visible_land_cover_zh": str(visible_land_cover_zh),
        "water_visible": water_visible,
        "dense_builtup_visible": dense_builtup_visible,
        "dense_vegetation_visible": dense_vegetation_visible,
        "bare_or_open_land_visible": bare_or_open_land_visible,
        "terrain_or_shadow_issue_visible": terrain_or_shadow_issue_visible,
        "remote_sensing_quality": quality,
        "site_screening_signal": signal,
        "human_review_required": human_review_required,
        "confidence": confidence,
        "reason_zh": str(reason_zh),
    }




def _build_prompt(record: dict[str, Any], rule_record: dict[str, Any] | None) -> str:
    rule_summary = {}
    if rule_record:
        rule_summary = {
            "rule_label": rule_record.get("verification_label"),
            "rule_label_zh": rule_record.get("verification_label_zh"),
            "water_suspect": rule_record.get("water_suspect"),
            "vegetation_suspect": rule_record.get("vegetation_suspect"),
            "builtup_or_hardened_surface_suspect": rule_record.get("builtup_or_hardened_surface_suspect"),
            "bare_or_open_land_suspect": rule_record.get("bare_or_open_land_suspect"),
            "human_review_required": rule_record.get("human_review_required"),
        }

    return (
        "请基于这张真实 Sentinel-2 RGB 遥感切片，对新能源场站候选区做保守核验。\n"
        "不要因为它是候选区就默认适合建设。只能根据图像可见信息判断。\n\n"
        f"项目类型: {record.get('project_type')}\n"
        f"筛选范围: {record.get('scope_kind')}\n"
        f"规则预核验结果: {json.dumps(rule_summary, ensure_ascii=False)}\n\n"
        "请只返回一个 JSON 对象，不要返回 Markdown，不要返回代码块。\n"
        "所有字段都必须出现，字段名必须严格使用下面这些英文 key。\n"
        "visible_land_cover_zh 和 reason_zh 必须用中文填写，不能留空。\n"
        "如果图像不清晰，也要写明不清晰原因。\n\n"
        "{\n"
        '  "visible_land_cover_zh": "用中文描述可见地表，例如植被、裸地、建筑、水体、道路、阴影等",\n'
        '  "water_visible": false,\n'
        '  "dense_builtup_visible": false,\n'
        '  "dense_vegetation_visible": false,\n'
        '  "bare_or_open_land_visible": false,\n'
        '  "terrain_or_shadow_issue_visible": false,\n'
        '  "remote_sensing_quality": "good",\n'
        '  "site_screening_signal": "uncertain",\n'
        '  "human_review_required": true,\n'
        '  "confidence": 0.5,\n'
        '  "reason_zh": "用中文说明判断原因，不超过80字"\n'
        "}\n\n"
        "site_screening_signal 只能取 favorable、uncertain、unfavorable。\n"
        "remote_sensing_quality 只能取 good、medium、poor。\n"
        "对新能源场站选址要保守判断；水体、密集建成区、明显阴影或质量差都应要求人工复核。"
    )


def run_vlm_verification(limit: int | None = None, timeout_sec: int = 120) -> dict[str, Any]:
    provider_config = _load_provider_config()

    acquisition_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4b_tile_acquisition_manifest_v1.json"
    )
    rule_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_rule_based_verification_v1.json"
    )

    acquisition = read_json(acquisition_path)
    rule_report = read_json(rule_path)

    acquired_records = [
        r for r in acquisition.get("records", [])
        if r.get("tile_acquisition_status") == "acquired"
    ]
    if limit is not None:
        acquired_records = acquired_records[:limit]

    rule_by_sample = {
        r.get("sample_id"): r for r in rule_report.get("records", [])
    }

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_dir = RUNTIME_ROOT / "remote_sensing"
    report_path = output_dir / "closed_loop_4c_vlm_verification_v1.json"
    csv_path = output_dir / "closed_loop_4c_vlm_verification_records_v1.csv"

    if not provider_config["provider_ready"]:
        report = {
            "vlm_verification_id": "closed_loop_4c_vlm_verification_v1",
            "created_at_utc": now,
            "vlm_verification_status": "blocked_provider_not_configured",
            "vlm_verification_status_zh": "未配置真实 VLM provider，不能执行视觉语言模型核验",
            "real_vlm_call_executed": False,
            "do_not_fake_vlm_result": True,
            "provider_config": provider_config,
            "tile_count_available": len(acquired_records),
            "verified_tile_count": 0,
            "failed_tile_count": 0,
            "records": [],
            "next_stage": "configure_real_vlm_provider_or_use_rule_based_report",
            "next_stage_zh": "配置真实视觉模型服务，或暂时使用规则核验报告进入人工复核。",
        }
        write_json(report_path, report)
        return {
            "vlm_verification_status": report["vlm_verification_status"],
            "vlm_verification_status_zh": report["vlm_verification_status_zh"],
            "real_vlm_call_executed": False,
            "tile_count_available": len(acquired_records),
            "verified_tile_count": 0,
            "failed_tile_count": 0,
            "json_report_path": str(report_path),
            "csv_records_path": str(csv_path),
            "required_env": provider_config["required_env"],
        }

    api_key = (
        os.getenv("GEOAGENT_VLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )

    records = []
    for item in acquired_records:
        sample_id = item.get("sample_id")
        tile_path = Path(item.get("registered_tile_path", ""))

        record = {
            "sample_id": sample_id,
            "project_type": item.get("project_type"),
            "scope_kind": item.get("scope_kind"),
            "registered_tile_path": str(tile_path),
            "vlm_status": "pending",
            "failure_reason": None,
        }

        last_exc: Exception | None = None

        for attempt in range(2):
            try:
                prompt = _build_prompt(item, rule_by_sample.get(sample_id))
                response = _call_openai_compatible_vlm(
                    base_url=provider_config["base_url"],
                    api_key=api_key,
                    model=provider_config["model"],
                    image_path=tile_path,
                    prompt=prompt,
                    timeout_sec=timeout_sec,
                )

                parsed = response["parsed_response"]
                normalized = _normalize_vlm_response(parsed)
                record.update(normalized)
                record["parsed_vlm_response"] = parsed
                record["vlm_status"] = "verified_by_vlm"
                record["raw_vlm_response"] = response["raw_response"]
                record["vlm_attempt_count"] = attempt + 1
                last_exc = None
                break

            except Exception as exc:
                last_exc = exc
                record["vlm_attempt_count"] = attempt + 1

        if last_exc is not None:
            record["vlm_status"] = "failed"
            record["failure_reason"] = f"{type(last_exc).__name__}: {last_exc}"

        records.append(record)

    verified_count = sum(1 for r in records if r["vlm_status"] == "verified_by_vlm")
    failed_count = sum(1 for r in records if r["vlm_status"] == "failed")

    if verified_count == len(records) and records:
        status = "completed"
        status_zh = "VLM 遥感核验完成"
    elif verified_count > 0:
        status = "partially_completed"
        status_zh = "VLM 遥感核验部分完成"
    else:
        status = "failed"

        status_zh = "VLM 遥感核验失败"

    human_review_count = sum(
        1 for r in records if r.get("human_review_required") is True
    )
    unfavorable_count = sum(
        1 for r in records if r.get("site_screening_signal") == "unfavorable"
    )
    favorable_count = sum(
        1 for r in records if r.get("site_screening_signal") == "favorable"
    )
    uncertain_count = sum(
        1 for r in records if r.get("site_screening_signal") == "uncertain"
    )

    report = {
        "vlm_verification_id": "closed_loop_4c_vlm_verification_v1",
        "created_at_utc": now,
        "vlm_verification_status": status,
        "vlm_verification_status_zh": status_zh,
        "verification_mode": "openai_compatible_vlm_on_real_sentinel2_tiles",
        "real_vlm_call_executed": verified_count > 0,
        "provider_config": provider_config,
        "source_acquisition_manifest": str(acquisition_path),
        "source_rule_report": str(rule_path),
        "tile_count": len(records),
        "verified_tile_count": verified_count,
        "failed_tile_count": failed_count,
        "human_review_required_count": human_review_count,
        "favorable_count": favorable_count,
        "uncertain_count": uncertain_count,
        "unfavorable_count": unfavorable_count,
        "records": records,
    }

    write_json(report_path, report)
    _write_csv(csv_path, records)

    return {
        "vlm_verification_status": status,
        "vlm_verification_status_zh": status_zh,
        "real_vlm_call_executed": verified_count > 0,
        "tile_count": len(records),
        "verified_tile_count": verified_count,
        "failed_tile_count": failed_count,
        "human_review_required_count": human_review_count,
        "favorable_count": favorable_count,
        "uncertain_count": uncertain_count,
        "unfavorable_count": unfavorable_count,
        "json_report_path": str(report_path),
        "csv_records_path": str(csv_path),
    }


def validate_vlm_verification() -> dict[str, Any]:
    report_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_vlm_verification_v1.json"
    )
    report = read_json(report_path)

    status = report.get("vlm_verification_status")

    if status == "blocked_provider_not_configured":
        validation_status = "passed_blocked_provider_not_configured"
    elif status in {"completed", "partially_completed", "failed"}:
        records = report.get("records", [])
        if report.get("tile_count") != len(records):
            raise RuntimeError("VLM tile_count does not match record count.")
        validation_status = "passed"
    else:
        raise RuntimeError(f"Invalid VLM verification status: {status}")

    validation = {
        "closed_loop_4c_vlm_verification_validation_status": validation_status,
        "source_report_path": str(report_path),
        "vlm_verification_status": status,
        "real_vlm_call_executed": report.get("real_vlm_call_executed"),
        "tile_count": report.get("tile_count", report.get("tile_count_available")),
        "verified_tile_count": report.get("verified_tile_count"),
        "failed_tile_count": report.get("failed_tile_count"),
        "next_stage": "closed_loop_4d_remote_sensing_evidence_fusion",
        "next_stage_zh": "下一步融合规则核验和 VLM 核验结果；若未配置 VLM，则仅融合规则核验结果。",
    }

    validation_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4c_vlm_verification_validation_v1.json"
    )
    write_json(validation_path, validation)

    result = dict(validation)
    result["validation_report_path"] = str(validation_path)
    return result

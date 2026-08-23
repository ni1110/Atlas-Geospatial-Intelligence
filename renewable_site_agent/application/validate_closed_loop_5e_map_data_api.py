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


def validate_map_data_api(base_url: str, timeout_sec: int) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    delivery_dir = RUNTIME_ROOT / "enterprise_delivery"
    errors: list[str] = []

    analyze_payload = {
        "query": "我想在北京市找光伏候选区域，要求避开水体，坡度小于10度",
        "execute": True,
        "include_remote_sensing_summary": True,
        "timeout_sec": timeout_sec,
    }

    analyze = post_json(
        f"{base_url}/v1/site-selection/analyze",
        payload=analyze_payload,
        timeout_sec=timeout_sec + 60,
    )

    job_id = analyze.get("dynamic_job", {}).get("job_id")
    execution = analyze.get("execution", {})
    candidate_count = execution.get("candidate_count")

    if not job_id:
        errors.append("Analyze API did not return job_id.")

    if analyze.get("service_status") != "completed":
        errors.append("Analyze API did not complete.")

    if execution.get("dynamic_execution_status") != "completed":
        errors.append("Dynamic GIS execution did not complete.")

    if int(candidate_count or 0) <= 0:
        errors.append("Analyze API returned no candidates.")

    map_data = {}
    if job_id:
        map_data = get_json(
            f"{base_url}/v1/jobs/{job_id}/map-data?include_geojson=false",
            timeout_sec=timeout_sec,
        )

        if map_data.get("service_status") != "completed":
            errors.append("Map-data API did not complete.")

        if map_data.get("map_data_status") != "ready":
            errors.append("Map-data API did not return ready status.")

        if int(map_data.get("candidate_count", 0)) <= 0:
            errors.append("Map-data API returned invalid candidate_count.")

        if int(map_data.get("feature_count", 0)) <= 0:
            errors.append("Map-data API returned invalid feature_count.")

        bbox = map_data.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            errors.append("Map-data API returned invalid bbox.")

        center = map_data.get("center")
        if not isinstance(center, dict) or "lng" not in center or "lat" not in center:
            errors.append("Map-data API returned invalid center.")

        style = map_data.get("recommended_layer_style", {})
        if style.get("geometry_type") != "polygon":
            errors.append("Map-data API did not return polygon style.")

        if not style.get("fill_color"):
            errors.append("Map-data API did not return fill_color.")

        if map_data.get("geojson") is not None:
            errors.append("Map-data API should omit geojson when include_geojson=false.")

    status = "passed" if not errors else "failed"
    status_zh = "闭环 5E 地图数据接口验收通过" if not errors else "闭环 5E 地图数据接口验收失败"

    report = {
        "closed_loop_5e_map_data_api_validation_id": "closed_loop_5e_map_data_api_validation_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "closed_loop_5e_map_data_api_validation_status": status,
        "closed_loop_5e_status_zh": status_zh,
        "base_url": base_url,
        "errors": errors,
        "job_id": job_id,
        "analyze_summary": {
            "service_status": analyze.get("service_status"),
            "execution_status": execution.get("dynamic_execution_status"),
            "candidate_count": candidate_count,
            "final_candidate_path": execution.get("final_candidate_path"),
        },
        "map_data_summary": {
            "service_status": map_data.get("service_status"),
            "map_data_status": map_data.get("map_data_status"),
            "candidate_count": map_data.get("candidate_count"),
            "feature_count": map_data.get("feature_count"),
            "bbox": map_data.get("bbox"),
            "center": map_data.get("center"),
            "coordinate_count": map_data.get("coordinate_count"),
            "recommended_map_action": map_data.get("recommended_map_action"),
            "recommended_layer_style": map_data.get("recommended_layer_style"),
            "human_review_required": map_data.get("human_review_required"),
        },
        "validated_api": "GET /v1/jobs/{job_id}/map-data",
        "validated_frontend_use_zh": "前端可以使用该接口获取候选区数量、bbox、center、GeoJSON 路径和推荐图层样式，从而实现地图自动跳转和候选区蓝色图层渲染。",
        "next_stage": "frontend_map_integration",
        "next_stage_zh": "下一步把 map-data API 接入前端网页地图。",
    }

    json_path = delivery_dir / "closed_loop_5e_map_data_api_validation_v1.json"
    md_path = delivery_dir / "closed_loop_5e_map_data_api_validation_report_v1.md"

    write_json(json_path, report)

    md = f"""
# Closed Loop 5E Map Data API Validation Report

- Status: `{status}`
- Status zh: {status_zh}
- Base URL: `{base_url}`

## What was validated

The API endpoint `GET /v1/jobs/{{job_id}}/map-data` was validated.

It returns map-ready information for a request-driven GIS job, including:

- candidate count;
- feature count;
- final candidate GeoJSON path;
- map bbox;
- map center;
- recommended fit bounds;
- recommended polygon layer style;
- human review warning.

## Main Result

- Job ID: `{job_id}`
- Candidate count: {map_data.get("candidate_count")}
- Feature count: {map_data.get("feature_count")}
- BBox: `{map_data.get("bbox")}`
- Center: `{map_data.get("center")}`
- Layer style: `{map_data.get("recommended_layer_style")}`

## Interpretation

The backend now exposes map-ready data for frontend visualization.

This explains the original map behavior:

Backend GeoJSON
→ map-data API
→ frontend fit bounds
→ frontend renders candidate polygons
→ blue candidate layer appears on the map

The current interface prepares the backend side of this visualization. The remaining step is to connect the frontend map code to this API.

## Next Stage

{report["next_stage_zh"]}
"""

    write_text(md_path, md)

    return {
        "closed_loop_5e_map_data_api_validation_status": status,
        "closed_loop_5e_status_zh": status_zh,
        "job_id": job_id,
        "candidate_count": map_data.get("candidate_count"),
        "feature_count": map_data.get("feature_count"),
        "bbox": map_data.get("bbox"),
        "center": map_data.get("center"),
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

    result = validate_map_data_api(
        base_url=args.base_url,
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

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


def validate_async_job_api(base_url: str, timeout_sec: int) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    delivery_dir = RUNTIME_ROOT / "enterprise_delivery"

    errors: list[str] = []

    # 1. Create a fresh API job.
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
    if not job_id:
        errors.append("Analyze API did not return job_id.")

    execution = analyze.get("execution", {})
    if execution.get("dynamic_execution_status") != "completed":
        errors.append("Analyze API dynamic execution did not complete.")

    if int(execution.get("candidate_count", 0)) <= 0:
        errors.append("Analyze API returned no candidates.")

    # 2. Query job status.
    job_status = {}
    if job_id:
        job_status = get_json(
            f"{base_url}/v1/jobs/{job_id}/status",
            timeout_sec=timeout_sec,
        )

        if job_status.get("service_status") != "completed":
            errors.append("Job status API did not complete.")

        if job_status.get("job_found") is not True:
            errors.append("Job status API did not find the job.")

        if job_status.get("final_candidate_exists") is not True:
            errors.append("Job status API did not find final candidate GeoJSON.")

        if int(job_status.get("candidate_count", 0)) <= 0:
            errors.append("Job status API returned no candidate count.")

    # 3. Trigger remote-sensing queue.
    trigger = {}
    trigger_id = None
    if job_id:
        trigger = post_json(
            f"{base_url}/v1/jobs/{job_id}/remote-sensing/trigger",
            payload={
                "sample_count": 3,
                "mode": "queued",
                "note": "5C validation trigger for API-created solar screening job.",
            },
            timeout_sec=timeout_sec,
        )

        trigger_id = trigger.get("trigger_id")

        if trigger.get("trigger_status") != "queued":
            errors.append("Remote-sensing trigger API did not return queued.")

        if not trigger_id:
            errors.append("Remote-sensing trigger API did not return trigger_id.")

        trigger_record_path = trigger.get("trigger_record_path")
        if not trigger_record_path or not Path(trigger_record_path).is_file():
            errors.append("Remote-sensing trigger record file does not exist.")

    # 4. Query trigger status.
    trigger_lookup = {}
    if trigger_id:
        trigger_lookup = get_json(
            f"{base_url}/v1/remote-sensing/triggers/{trigger_id}",
            timeout_sec=timeout_sec,
        )

        if trigger_lookup.get("service_status") != "completed":
            errors.append("Trigger lookup API did not complete.")

        if trigger_lookup.get("trigger", {}).get("trigger_status") != "queued":
            errors.append("Trigger lookup API did not return queued trigger.")

    status = "passed" if not errors else "failed"
    status_zh = "闭环 5C 异步任务状态与遥感触发接口验收通过" if not errors else "闭环 5C 异步任务状态与遥感触发接口验收失败"

    report = {
        "closed_loop_5c_async_job_api_validation_id": "closed_loop_5c_async_job_api_validation_v1",
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "closed_loop_5c_async_job_api_validation_status": status,
        "closed_loop_5c_status_zh": status_zh,
        "base_url": base_url,
        "errors": errors,
        "job_id": job_id,
        "trigger_id": trigger_id,
        "analyze_summary": {
            "service_status": analyze.get("service_status"),
            "execution_status": execution.get("dynamic_execution_status"),
            "candidate_count": execution.get("candidate_count"),
            "final_candidate_path": execution.get("final_candidate_path"),
        },
        "job_status_summary": {
            "service_status": job_status.get("service_status"),
            "job_found": job_status.get("job_found"),
            "execution_status": job_status.get("execution_status"),
            "candidate_count": job_status.get("candidate_count"),
            "final_candidate_exists": job_status.get("final_candidate_exists"),
            "final_candidate_path": job_status.get("final_candidate_path"),
        },
        "trigger_summary": {
            "trigger_status": trigger.get("trigger_status"),
            "trigger_record_path": trigger.get("trigger_record_path"),
            "candidate_count": trigger.get("candidate_count"),
            "source_final_candidate_path": trigger.get("source_final_candidate_path"),
        },
        "trigger_lookup_summary": {
            "service_status": trigger_lookup.get("service_status"),
            "trigger_status": trigger_lookup.get("trigger", {}).get("trigger_status"),
            "do_not_fake_imagery": trigger_lookup.get("trigger", {}).get("do_not_fake_imagery"),
            "do_not_auto_approve": trigger_lookup.get("trigger", {}).get("do_not_auto_approve"),
        },
        "next_stage": "closed_loop_5d_final_enterprise_validation",
        "next_stage_zh": "下一步进行闭环 5 企业级交付总体验收",
    }

    json_path = delivery_dir / "closed_loop_5c_async_job_api_validation_v1.json"
    md_path = delivery_dir / "closed_loop_5c_async_job_api_validation_report_v1.md"

    write_json(json_path, report)

    md = f"""
# Closed Loop 5C Async Job API Validation Report

- Status: `{status}`
- Status zh: {status_zh}
- Base URL: `{base_url}`

## What was validated

1. API can create and execute a request-driven GIS job.
2. API can query job status by `job_id`.
3. API can trigger a queued remote-sensing verification record.
4. API can query the trigger record by `trigger_id`.

## Main Result

- Job ID: `{job_id}`
- Trigger ID: `{trigger_id}`
- Candidate count: {job_status.get("candidate_count")}
- Final candidate exists: {job_status.get("final_candidate_exists")}
- Trigger status: `{trigger.get("trigger_status")}`

## Interpretation

The system now supports an enterprise-style API workflow:

User request
→ dynamic GIS job
→ job status query
→ remote-sensing verification trigger
→ trigger record query

The remote-sensing trigger is queued instead of forcing heavy Sentinel-2 and VLM inference inside the synchronous API request.

## Next Stage

{report["next_stage_zh"]}
"""

    write_text(md_path, md)

    return {
        "closed_loop_5c_async_job_api_validation_status": status,
        "closed_loop_5c_status_zh": status_zh,
        "job_id": job_id,
        "trigger_id": trigger_id,
        "candidate_count": job_status.get("candidate_count"),
        "final_candidate_exists": job_status.get("final_candidate_exists"),
        "trigger_status": trigger.get("trigger_status"),
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

    result = validate_async_job_api(
        base_url=args.base_url,
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

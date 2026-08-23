"""Create a verified execution plan record for the available-data workflow."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_available_data_job import validate_job
from renewable_site_agent.agent.tool_registry import get_registered_tool


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class ExecutionPlanError(RuntimeError):
    """Raised when execution plan creation fails."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExecutionPlanError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def artifact(path: str, role: str) -> dict[str, str]:
    return {
        "role": role,
        "path": path,
    }


def create_execution_plan(job_id: str) -> dict[str, Any]:
    job_dir = RUNTIME_ROOT / "jobs" / job_id
    final_dir = job_dir / "final"
    agent_dir = job_dir / "agent"

    decision_log_path = final_dir / "screening_decision_log.json"
    decision_log = read_json(decision_log_path)

    validation = validate_job(job_id)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    plan = {
        "plan_id": f"available-data-gis-plan-{job_id}-v1",
        "job_id": job_id,
        "plan_version": 1,
        "created_at_utc": now,
        "planning_mode": "deterministic_template_for_available_data_workflow",
        "plan_status": "verified_completed",
        "important_boundary": (
            "This plan records and verifies an available-data GIS workflow. "
            "The agent layer records tool order and validation status. "
            "GIS outputs are produced by deterministic spatial programs, not by LLM guessing."
        ),
        "completed_steps": [
            {
                "step_id": "step_01_parse_request",
                "tool_or_script": "site_selection_request_parser",
                "status": "completed",
                "description": "Chinese natural-language site-selection request was converted into a structured SiteSelectionJob request.",
                "outputs": [
                    artifact("runtime/site_jobs.db", "job_store"),
                ],
            },
            {
                "step_id": "step_02_load_boundary",
                "tool_or_script": "administrative boundary adapter",
                "status": "completed",
                "description": "Loaded real Beijing administrative boundary.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/administrative_boundary.geojson", "administrative_boundary"),
                ],
            },
            {
                "step_id": "step_03_exclude_water",
                "tool_or_script": "water exclusion GIS tool",
                "status": "completed",
                "description": "Subtracted OSM/Geofabrik water polygons from Beijing boundary.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/water_exclusion.geojson", "water_exclusion"),
                    artifact(f"runtime/jobs/{job_id}/non_water_area.geojson", "non_water_area"),
                    artifact(f"runtime/jobs/{job_id}/candidate_sites.geojson", "initial_candidate_sites"),
                ],
            },
            {
                "step_id": "step_04_prepare_dem",
                "tool_or_script": "renewable_site_agent.application.prepare_terrain_dem",
                "status": "completed",
                "description": "Merged, clipped, and reprojected Copernicus GLO-30 DEM to EPSG:32650.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/terrain/beijing_dem_utm50.tif", "prepared_dem"),
                    artifact(f"runtime/jobs/{job_id}/terrain/terrain_metadata.json", "terrain_metadata"),
                ],
            },
            {
                "step_id": "step_05_calculate_slope",
                "tool_or_script": "renewable_site_agent.application.calculate_slope",
                "status": "completed",
                "description": "Calculated slope degrees and slope < 8 degree mask from DEM.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_degrees.tif", "slope_raster"),
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_suitable_mask_8deg.tif", "slope_suitable_mask"),
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_metadata.json", "slope_metadata"),
                ],
            },
            {
                "step_id": "step_06_vectorize_slope",
                "tool_or_script": "renewable_site_agent.application.vectorize_slope_mask",
                "status": "completed",
                "description": "Converted slope < 8 degree raster mask into a vector exclusion/suitability layer.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_suitable_area.geojson", "slope_suitable_area"),
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_vector_metadata.json", "slope_vector_metadata"),
                ],
            },
            {
                "step_id": "step_07_apply_slope_screening",
                "tool_or_script": "renewable_site_agent.application.apply_slope_screening",
                "status": "completed",
                "description": "Intersected non-water candidate polygons with slope-suitable area.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_screened_candidate_sites.geojson", "slope_screened_candidates"),
                    artifact(f"runtime/jobs/{job_id}/terrain/slope_screening_metadata.json", "slope_screening_metadata"),
                ],
            },
            {
                "step_id": "step_08_prepare_protected_areas",
                "tool_or_script": "renewable_site_agent.application.prepare_protected_areas",
                "status": "completed",
                "description": "Prepared Beijing protected-area exclusion polygon from local OSM/Geofabrik layer.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/protected_areas/protected_areas_exclusion.geojson", "protected_area_exclusion"),
                    artifact(f"runtime/jobs/{job_id}/protected_areas/protected_areas_metadata.json", "protected_area_metadata"),
                ],
            },
            {
                "step_id": "step_09_apply_protected_area_screening",
                "tool_or_script": "renewable_site_agent.application.apply_protected_area_screening",
                "status": "completed",
                "description": "Subtracted protected-area exclusion layer from slope-screened candidates.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/protected_areas/protected_area_screened_candidate_sites.geojson", "protected_area_screened_candidates"),
                    artifact(f"runtime/jobs/{job_id}/protected_areas/protected_area_screening_metadata.json", "protected_area_screening_metadata"),
                ],
            },
            {
                "step_id": "step_10_finalize_available_data_candidates",
                "tool_or_script": "renewable_site_agent.application.finalize_available_data_candidates",
                "status": "completed",
                "description": "Finalized available-data preliminary candidate set and recorded skipped constraints.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/final/available_data_screened_candidate_sites.geojson", "available_data_final_candidates"),
                    artifact(f"runtime/jobs/{job_id}/final/screening_decision_log.json", "screening_decision_log"),
                ],
            },
            {
                "step_id": "step_11_generate_report",
                "tool_or_script": "renewable_site_agent.application.generate_available_data_report",
                "status": "completed",
                "description": "Generated available-data HTML report with evidence and reproducibility records.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/final/available_data_screening_report.html", "html_report"),
                    artifact(f"runtime/jobs/{job_id}/final/available_data_screening_report.metadata.json", "report_metadata"),
                ],
            },
            {
                "step_id": "step_12_build_map_preview",
                "tool_or_script": "renewable_site_agent.application.build_map_preview",
                "status": "completed",
                "description": "Generated lightweight map preview using final 608 available-data candidate polygons.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/map_preview/candidate_preview.geojson", "candidate_map_preview"),
                    artifact(f"runtime/jobs/{job_id}/map_preview/preview_metadata.json", "map_preview_metadata"),
                ],
            },
            {
                "step_id": "step_13_validate_outputs",
                "tool_or_script": "renewable_site_agent.application.validate_available_data_job",
                "status": "completed",
                "description": "Validated final candidates, map preview, water overlap, protected-area overlap, and skipped constraints.",
                "outputs": [
                    artifact(f"runtime/jobs/{job_id}/agent/execution_plan_v1.verification.json", "verification_record"),
                ],
            },
        ],
        "skipped_steps": decision_log.get("skipped_constraints", []),
        "final_result": {
            "candidate_count": validation["final_candidate_count"],
            "total_area_km2": validation["total_area_km2"],
            "status": "preliminary_not_final_recommendation",
            "final_candidate_path": decision_log["latest_candidate_output"]["path"],
        },
        "verification_summary": {
            "validation_status": validation["validation_status"],
            "final_candidate_count": validation["final_candidate_count"],
            "preview_candidate_count": validation["preview_candidate_count"],
            "water_overlap_km2": validation["water_overlap_km2"],
            "protected_overlap_km2": validation["protected_overlap_km2"],
            "skipped_constraints": validation["skipped_constraints"],
        },
        "next_stage_note": (
            "This is not yet a full autonomous Agent planning loop. "
            "It is the first execution-plan and verifier record for the completed available-data GIS workflow. "
            "The next step is to turn these deterministic scripts into registered tools and let an Agent assemble the plan."
        ),
    }

    step_tool_ids = {
        "step_01_parse_request": "parse_site_selection_request",
        "step_02_load_boundary": "load_administrative_boundary",
        "step_03_exclude_water": "exclude_water_bodies",
        "step_04_prepare_dem": "prepare_terrain_dem",
        "step_05_calculate_slope": "calculate_slope",
        "step_06_vectorize_slope": "vectorize_slope_mask",
        "step_07_apply_slope_screening": "apply_slope_screening",
        "step_08_prepare_protected_areas": "prepare_protected_areas",
        "step_09_apply_protected_area_screening": "apply_protected_area_screening",
        "step_10_finalize_available_data_candidates": "finalize_available_data_candidates",
        "step_11_generate_report": "generate_available_data_report",
        "step_12_build_map_preview": "build_map_preview",
        "step_13_validate_outputs": "validate_available_data_job",
    }

    for step in plan["completed_steps"]:
        step_id = step["step_id"]
        registered_tool_id = step_tool_ids.get(step_id)
        if registered_tool_id is None:
            raise ExecutionPlanError(f"Missing registered tool binding for step: {step_id}")

        registered_tool = get_registered_tool(registered_tool_id)
        step["registered_tool_id"] = registered_tool.tool_id
        step["registered_tool_module"] = registered_tool.module
        step["registered_tool_category"] = registered_tool.category
        step["registered_tool_deterministic"] = registered_tool.deterministic

    plan_path = agent_dir / "execution_plan_v1.json"
    verification_path = agent_dir / "execution_plan_v1.verification.json"

    verification = {
        "job_id": job_id,
        "plan_id": plan["plan_id"],
        "verified_at_utc": now,
        "verification_status": validation["validation_status"],
        "checks": validation,
        "verifier": "renewable_site_agent.application.validate_available_data_job",
    }

    write_json_atomic(plan_path, plan)
    write_json_atomic(verification_path, verification)

    return {
        "job_id": job_id,
        "plan_path": str(plan_path),
        "verification_path": str(verification_path),
        "plan_status": plan["plan_status"],
        "completed_step_count": len(plan["completed_steps"]),
        "skipped_step_count": len(plan["skipped_steps"]),
        "validation_status": validation["validation_status"],
        "final_candidate_count": validation["final_candidate_count"],
        "preview_candidate_count": validation["preview_candidate_count"],
        "water_overlap_km2": validation["water_overlap_km2"],
        "protected_overlap_km2": validation["protected_overlap_km2"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = create_execution_plan(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

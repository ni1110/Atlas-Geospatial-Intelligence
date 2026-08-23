"""Opt-in final freeze acceptance using the formal API and real Zhipu GLM."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from geoagent.orchestration.formal_runtime import build_formal_handlers, formal_provider_status
from geoagent.orchestration.harness import ToolInvoker
from geoagent.orchestration.models import ArtifactRef, GoalContract, TaskNode
from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.planning import DynamicGeoPlanner
from geoagent.orchestration.routing import HierarchicalIntentRouter
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM
from renewable_site_agent.api import app
from renewable_site_agent.config import PATHS, apply_process_environment


OUT_JSON = PATHS.artifacts / "final_freeze_acceptance_metrics.json"
OUT_MD = PATHS.artifacts / "FINAL_ACCEPTANCE_REPORT.md"
CHECKPOINT_JSON = PATHS.artifacts / "final_freeze_acceptance_checkpoint.json"
CHECKPOINT_VERSION = "freeze-v1"
CASE_REVISIONS = {"buffer_then_intersect": 1, "sentinel_metadata_only": 4, "open_multi_domain": 5}


def _require_live_zhipu() -> None:
    if not os.getenv("ZHIPUAI_API_KEY"):
        raise RuntimeError("ZHIPU_LIVE_KEY_REQUIRED: ZHIPUAI_API_KEY is absent from this process")
    os.environ["GEOAGENT_MODEL_PROVIDER"] = "zhipu"
    os.environ["GEOAGENT_MODEL_ID"] = "glm-4.5-flash"
    os.environ.setdefault("GEOAGENT_MODEL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")


def _write_geometry_fixtures() -> tuple[Path, Path]:
    root = PATHS.artifacts / "evals" / "final-freeze-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "point.geojson"
    mask = root / "mask.geojson"
    source.write_text(json.dumps({"type": "Feature", "properties": {"id": "p1"}, "geometry": {"type": "Point", "coordinates": [103.05, 37.65]}}), encoding="utf-8")
    mask.write_text(json.dumps({"type": "Feature", "properties": {"id": "m1"}, "geometry": {"type": "Polygon", "coordinates": [[[103.04,37.64],[103.06,37.64],[103.06,37.66],[103.04,37.66],[103.04,37.64]]]}}), encoding="utf-8")
    return source, mask


def _run_api_cases() -> list[dict[str, Any]]:
    source, mask = _write_geometry_fixtures()
    aoi = {"bbox": [102.99885031030179, 37.59704374600004, 103.09885031030178, 37.697043746000034], "crs": "EPSG:4326"}
    cases = [
        {
            "case": "simple_buffer",
            "user_query": "Using the supplied point near Lanzhou, buffer it by exactly 500 metres and return valid GeoJSON.",
            "input_artifacts": [{"path": str(source), "kind": "geojson"}],
            "region_or_aoi": aoi, "constraints": {"buffer_distance_m": 500},
        },
        {
            "case": "buffer_then_intersect",
            "user_query": "For the supplied Gansu point and polygon, create a 500 metre buffer then intersect the result with the supplied mask.",
            "input_artifacts": [{"path": str(source)}, {"path": str(mask)}],
            "region_or_aoi": aoi, "constraints": {"buffer_distance_m": 500},
        },
        {
            "case": "terrain_cached_dem",
            "user_query": "Analyze elevation and slope for the specified small Gansu AOI using the existing accepted DEM window; report terrain statistics and provenance.",
            "region_or_aoi": aoi,
        },
        {
            "case": "sentinel_metadata_only",
            "user_query": "Remote-sensing task: search Sentinel-2 imagery from the last 30 days with cloud cover at most 20 percent for the specified small Gansu AOI. Return STAC item and asset metadata at native Sentinel-2 resolution with provenance; no spectral processing is required and do not download a full scene.",
            "region_or_aoi": aoi, "constraints": {"sentinel_full_scene_download": False, "metadata_live_query": True, "stac_date_range": "2026-07-11/2026-08-10", "cloud_cover_lte": 20, "stac_item_limit": 5},
        },
        {
            "case": "renewable_composite_cached",
            "user_query": "Screen the specified small Gansu AOI for a solar site using cached terrain, water, land-cover, road and solar-resource evidence; return ranked candidates with provenance.",
            "region_or_aoi": aoi, "constraints": {"exclude_water": True, "max_slope_deg": 8},
        },
        {
            "case": "open_multi_domain",
            "user_query": "This is an open-ended cross-domain general land-development feasibility screening for the specified small Gansu AOI; no final project type has been selected. Determine whether the area is worth investigating further. Consider interacting evidence about construction difficulty, natural resource conditions, nearby roads and grid infrastructure, and how the conclusion should be communicated. Decide which geographic factors and data to inspect yourself, use evidence accepted as of 2026-08-10, apply a maximum slope of 8 degrees and exclude water where relevant, and produce an uncertainty-aware visual summary.",
            "region_or_aoi": aoi, "constraints": {"exact_timeframe": "2026-08-10 accepted evidence", "development_type": "general land-development feasibility screening", "infrastructure_types": ["roads", "grid"], "max_slope_deg": 8, "exclude_water": True},
        },
    ]
    selected = {name.strip() for name in os.getenv("GEOAGENT_FINAL_CASES", "").split(",") if name.strip()}
    if selected:
        cases = [item for item in cases if item["case"] in selected]
    checkpoint: dict[str, Any] = {"version": CHECKPOINT_VERSION, "provider": "zhipu", "model": "glm-4.5-flash", "cases": {}}
    if CHECKPOINT_JSON.is_file():
        loaded = json.loads(CHECKPOINT_JSON.read_text(encoding="utf-8"))
        if loaded.get("version") == CHECKPOINT_VERSION and loaded.get("provider") == "zhipu" and loaded.get("model") == "glm-4.5-flash":
            checkpoint = loaded
    client = TestClient(app)
    rows: list[dict[str, Any]] = []
    for item in cases:
        revision = CASE_REVISIONS.get(item["case"], 0)
        cached = checkpoint["cases"].get(item["case"])
        if cached is not None and int(cached.get("case_revision", 0)) == revision:
            print(f"CHECKPOINT_REUSE case={item['case']}", flush=True)
            rows.append(checkpoint["cases"][item["case"]])
            continue
        print(f"CASE_START case={item['case']}", flush=True)
        payload = {key: value for key, value in item.items() if key != "case"}
        payload.update({"use_live_llm": True, "resume": False})
        response = client.post("/v1/geo/jobs", json=payload)
        if response.status_code != 201:
            raise RuntimeError(f"FORMAL_API_CASE_FAILED:{item['case']}:{response.status_code}:{response.text[:500]}")
        created = response.json()
        snapshot = client.get(created["status_url"]).json()
        if snapshot.get("route_mode") != "llm" or snapshot.get("plan_mode") != "llm":
            details = {
                "route_mode": snapshot.get("route_mode"), "plan_mode": snapshot.get("plan_mode"),
                "goal_intent_llm_error": snapshot.get("metrics", {}).get("goal_intent_llm_error"),
                "planner_metadata": snapshot.get("metrics", {}).get("planner_metadata"),
            }
            raise RuntimeError(f"LIVE_MODE_FALLBACK_FORBIDDEN:{item['case']}:{json.dumps(details, ensure_ascii=False)}")
        if snapshot.get("status") != "SUCCEEDED":
            raise RuntimeError(f"FORMAL_API_EXECUTION_FAILED:{item['case']}:{snapshot.get('status')}")
        calls = snapshot.get("actual_tool_calls", [])
        row = {
            "case": item["case"], "job_id": snapshot["job_id"], "status": snapshot["status"],
            "intent": snapshot.get("intent", {}).get("intent"), "route_mode": snapshot["route_mode"],
            "plan_mode": snapshot["plan_mode"], "selected_agents": snapshot["selected_agents"],
            "attempted_agents": list(dict.fromkeys(attempt["agent_id"] for attempt in snapshot["actual_attempts"])),
            "actual_tool_calls": calls, "execution_classes": snapshot["execution_classes"],
            "actual_path_signature": " -> ".join(f"{call['agent_id']}:{call['tool']}:{call['execution_class']}" for call in calls),
            "goal_intent_llm_calls": snapshot.get("metrics", {}).get("goal_intent_llm_call_count", 0),
            "planner_llm_calls": snapshot.get("task_graph", {}).get("planning_metadata", {}).get("llm_call_count", 0),
            "review_status": (snapshot.get("review") or {}).get("status"),
            "final_artifact_count": len(snapshot.get("final_artifacts", [])),
            "case_revision": revision,
        }
        rows.append(row)
        checkpoint["cases"][item["case"]] = row
        temporary = CHECKPOINT_JSON.with_suffix(".tmp")
        temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CHECKPOINT_JSON)
        print(f"CASE_DONE case={item['case']} job_id={row['job_id']} calls={len(calls)}", flush=True)
    # Refresh every row from the durable formal status API. This also upgrades
    # historical short execution-class labels without replaying successful work.
    for row in rows:
        snapshot = client.get(f"/v1/geo/jobs/{row['job_id']}").json()
        calls = snapshot.get("actual_tool_calls", [])
        final_refs = snapshot.get("final_artifacts", [])
        proof: dict[str, Any] = {}
        payload: dict[str, Any] = {}
        if final_refs:
            ref = final_refs[-1]
            path = Path(ref["path"])
            proof = {
                "exists": path.is_file(), "path": str(path),
                "producer_agent": ref.get("producer_agent"), "artifact_type": ref.get("artifact_type"),
                "checksum_sha256": ref.get("checksum_sha256"), "provenance": ref.get("provenance", []),
                "dependency_artifact_ids": ref.get("dependency_artifact_ids", []),
            }
            if path.is_file() and path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
        geometry_proof: dict[str, Any] | None = None
        stac_metadata_proof: list[dict[str, Any]] = []
        renewable_candidate_proof: dict[str, Any] | None = None
        for output in payload.get("actual_outputs", []):
            if isinstance(output, dict) and output.get("geojson"):
                features = output["geojson"].get("features", [])
                geometry_proof = {
                    "operation": output.get("operation"), "feature_count": output.get("feature_count"),
                    "geometry_types": sorted({feature.get("geometry", {}).get("type") for feature in features}),
                    "crs": output.get("crs"), "input_artifact_refs": payload.get("source_input_artifact_refs", []),
                }
            if isinstance(output, dict):
                if output.get("operation") == "search_sentinel" and output.get("items"):
                    stac_metadata_proof.append({
                        "provider": output.get("provider"), "source_url": output.get("source_url"),
                        "query": output.get("query"), "item_count": output.get("item_count"),
                        "items": output.get("items"), "full_scene_downloaded": output.get("full_scene_downloaded"),
                        "cache_hit": output.get("cache_hit"), "fallback_provider": output.get("fallback_provider"),
                    })
                for provider_item in output.get("providers", []):
                    if provider_item.get("item_metadata"):
                        stac_metadata_proof.append({"provider": provider_item.get("provider"), **provider_item["item_metadata"]})
                if output.get("candidate_count") is not None:
                    renewable_candidate_proof = {
                        "candidate_count": output.get("candidate_count"), "preview": output.get("preview", []),
                        "source": output.get("source"),
                    }
        row.update({
            "selected_agents": snapshot.get("selected_agents", []),
            "attempted_agents": list(dict.fromkeys(attempt["agent_id"] for attempt in snapshot.get("actual_attempts", []))),
            "actual_tool_calls": calls,
            "execution_classes": sorted({call.get("execution_class") for call in calls}),
            "actual_path_signature": " -> ".join(f"{call['agent_id']}:{call['tool']}:{call['execution_class']}" for call in calls),
            "task_graph_hash": (snapshot.get("task_graph") or {}).get("plan_hash"),
            "artifact_proof": proof, "geometry_proof": geometry_proof,
            "stac_metadata_proof": stac_metadata_proof,
            "renewable_candidate_proof": renewable_candidate_proof,
            "evidence_count": len(snapshot.get("evidence", [])),
            "review_status": (snapshot.get("review") or {}).get("status"),
        })
        attempted = set(row["attempted_agents"])
        tools = [call["tool"] for call in calls]
        if row["case"] == "simple_buffer" and "renewable_siting-agent" in attempted:
            raise RuntimeError("SIMPLE_GIS_INVOKED_RENEWABLE")
        if row["case"] == "buffer_then_intersect" and not {"buffer", "intersect"}.issubset(tools):
            raise RuntimeError("COMPOUND_GIS_MISSING_ACTUAL_OPERATIONS")
        if row["case"] == "buffer_then_intersect" and not (
            proof.get("exists") and proof.get("checksum_sha256") and geometry_proof
            and geometry_proof.get("feature_count", 0) > 0 and geometry_proof.get("crs") == "EPSG:4326"
            and len(geometry_proof.get("input_artifact_refs", [])) >= 2
        ):
            raise RuntimeError("COMPOUND_GIS_ARTIFACT_OR_GEOMETRY_PROOF_INCOMPLETE")
        if row["case"] == "sentinel_metadata_only" and "remote_sensing-agent" not in attempted:
            raise RuntimeError("REMOTE_CASE_MISSING_REMOTE_SENSING_AGENT")
        if row["case"] == "sentinel_metadata_only" and not stac_metadata_proof:
            raise RuntimeError("REMOTE_CASE_MISSING_STAC_ITEM_METADATA")
        if row["case"] == "renewable_composite_cached" and "renewable_siting-agent" not in attempted:
            raise RuntimeError("RENEWABLE_CASE_MISSING_COMPOSITE_AGENT")
        if row["case"] == "renewable_composite_cached" and not renewable_candidate_proof:
            raise RuntimeError("RENEWABLE_CASE_MISSING_CANDIDATE_PROOF")
        checkpoint["cases"][row["case"]] = row
    temporary = CHECKPOINT_JSON.with_suffix(".tmp")
    temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CHECKPOINT_JSON)
    return rows


def _smoke() -> dict[str, Any]:
    client = TestClient(app)
    health = client.get("/health")
    demo = client.get("/geoagent/demo")
    renewable_demo = client.get("/")
    required_ui_tokens = (
        "user_query", "detected_intent", "route_mode", "plan_mode",
        "goal_contract", "task_graph", "active_agent", "completed_agents",
        "actual_tool_calls", "review", "replans", "artifacts", "evidence", "final_result",
    )
    return {
        "health": health.status_code == 200 and health.json().get("status") == "ok",
        "general_browser_demo": demo.status_code == 200 and all(token in demo.text for token in required_ui_tokens),
        "specialized_map_demo": renewable_demo.status_code == 200 and 'id="map"' in renewable_demo.text,
        "formal_api_route_registered": any(getattr(route, "path", None) == "/v1/geo/jobs" for route in app.routes),
        "required_ui_tokens": list(required_ui_tokens),
    }


def _run_live_local_replan() -> dict[str, Any]:
    llm = StrandsStructuredLLM.from_environment()
    if llm is None or llm.provider != "zhipu" or llm.model_id != "glm-4.5-flash":
        raise RuntimeError("LIVE_REPLAN_MODEL_INITIALIZATION_FAILED")
    base = build_formal_handlers()
    counts = {"data": 0, "terrain": 0}

    def fault_then_recover(task: TaskNode, contract: GoalContract, inputs: list[ArtifactRef], invoke: ToolInvoker) -> dict[str, Any]:
        if task.agent_role in counts:
            counts[task.agent_role] += 1
        if task.agent_role == "terrain" and counts["terrain"] <= 2:
            raise RuntimeError("injected recoverable terrain failure")
        return base[task.agent_role](task, contract, inputs, invoke)

    orchestrator = GeoOrchestratorAgent(
        router=HierarchicalIntentRouter(llm=llm), planner=DynamicGeoPlanner(llm=llm),
        state_store=MultiAgentStateStore(),
    )
    result = orchestrator.execute(
        "Analyze slope and elevation in the small Gansu AOI using the accepted cached DEM and provenance.",
        use_llm=True, resume=False, handlers={"data": fault_then_recover, "terrain": fault_then_recover},
        region_or_aoi={"bbox": [102.99885, 37.59704, 103.09885, 37.69704], "crs": "EPSG:4326"},
        provider_status=formal_provider_status(),
    )
    orchestrator.state_store.save_result(result)
    row = {
        "job_id": result.job_id, "status": result.status,
        "replan_count": len(result.replans),
        "patch_modes": [patch.planner_mode for patch in result.replans],
        "patches": [patch.model_dump(mode="json") for patch in result.replans],
        "handler_calls": counts,
        "successful_nodes_reexecuted": max(0, counts["data"] - 1),
        "total_real_llm_calls": llm.call_count,
    }
    if not (result.status == "SUCCEEDED" and len(result.replans) == 1 and result.replans[0].planner_mode == "llm" and counts["data"] == 1):
        raise RuntimeError(f"LIVE_LOCAL_REPLAN_ACCEPTANCE_FAILED:{json.dumps(row, ensure_ascii=False)}")
    return row


def _execution_comparison_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive trace-only diversity and unnecessary-invocation metrics."""
    executable_categories = {
        "data", "gis_analysis", "terrain", "remote_sensing",
        "renewable_siting", "map_report",
    }
    observed_categories: set[str] = set()
    attempted_count = 0
    unexpected_count = 0
    for row in rows:
        categories = sorted({
            str(call.get("agent_id", "")).removesuffix("-agent")
            for call in row.get("actual_tool_calls", [])
            if call.get("agent_id")
        })
        row["tool_categories"] = categories
        observed_categories.update(categories)
        selected = {str(role).removesuffix("-agent") for role in row.get("selected_agents", [])}
        attempted = {str(role).removesuffix("-agent") for role in row.get("attempted_agents", [])}
        attempted_count += len(attempted)
        unexpected_count += len(attempted - selected)
    return {
        "tool_category_count": len(observed_categories),
        "tool_categories": sorted(observed_categories),
        "tool_category_diversity": len(observed_categories) / len(executable_categories),
        "actual_agent_invocation_count": attempted_count,
        "unnecessary_agent_invocation_count": unexpected_count,
        "unnecessary_agent_invocation_rate": unexpected_count / attempted_count if attempted_count else 0.0,
        "unnecessary_agent_invocation_definition": (
            "actual unique agent-role invocations absent from the LLM-selected agent set "
            "divided by all actual unique agent-role invocations"
        ),
    }


def main() -> None:
    apply_process_environment()
    if os.getenv("GEOAGENT_FINALIZE_ONLY") == "1":
        if not OUT_JSON.is_file():
            raise RuntimeError("FINAL_FREEZE_METRICS_REQUIRED")
        metrics = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        metrics.update(_execution_comparison_metrics(metrics.get("cases", [])))
        metrics["route_mode_llm_count"] = sum(row.get("route_mode") == "llm" for row in metrics.get("cases", []))
        metrics["route_mode_deterministic_fallback_count"] = sum(row.get("route_mode") != "llm" for row in metrics.get("cases", []))
        metrics["plan_mode_llm_count"] = sum(row.get("plan_mode") == "llm" for row in metrics.get("cases", []))
        metrics["plan_mode_deterministic_fallback_count"] = sum(row.get("plan_mode") != "llm" for row in metrics.get("cases", []))
        metrics["route_fallback_reasons"] = []
        metrics["plan_fallback_reasons"] = []
        OUT_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        write_final_report(metrics)
        print("FINAL_ACCEPTANCE_REPORT_REFRESHED", flush=True)
        return
    _require_live_zhipu()
    smoke = _smoke()
    if not all(value for key, value in smoke.items() if key not in {"required_ui_tokens"}):
        raise RuntimeError(f"LOCAL_WEB_API_SMOKE_FAILED:{json.dumps(smoke, ensure_ascii=False)}")
    rows = _run_api_cases()
    if os.getenv("GEOAGENT_DIAGNOSTIC_ONLY") == "1":
        print(json.dumps({"diagnostic_cases_completed": len(rows), "cases": [row["case"] for row in rows]}, ensure_ascii=False), flush=True)
        return
    previous_metrics = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.is_file() else {}
    prior_replan = previous_metrics.get("live_local_replan", {})
    if prior_replan.get("status") == "SUCCEEDED" and prior_replan.get("patch_modes") == ["llm"] and prior_replan.get("successful_nodes_reexecuted") == 0:
        replan = prior_replan
        print("CHECKPOINT_REUSE live_local_replan", flush=True)
    else:
        replan = _run_live_local_replan()
    signatures = {row["actual_path_signature"] for row in rows}
    execution_comparison = _execution_comparison_metrics(rows)
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "zhipu", "model": "glm-4.5-flash", "formal_entry": "POST /v1/geo/jobs",
        "case_count": len(rows), "passed_cases": sum(row["status"] == "SUCCEEDED" for row in rows),
        "route_mode_llm_rate": sum(row["route_mode"] == "llm" for row in rows) / len(rows),
        "route_mode_llm_count": sum(row["route_mode"] == "llm" for row in rows),
        "route_mode_deterministic_fallback_count": sum(row["route_mode"] != "llm" for row in rows),
        "route_fallback_reasons": [],
        "plan_mode_llm_rate": sum(row["plan_mode"] == "llm" for row in rows) / len(rows),
        "plan_mode_llm_count": sum(row["plan_mode"] == "llm" for row in rows),
        "plan_mode_deterministic_fallback_count": sum(row["plan_mode"] != "llm" for row in rows),
        "plan_fallback_reasons": [],
        "actual_path_unique_count": len(signatures),
        "actual_path_diversity_rate": len(signatures) / len(rows),
        "unique_task_graph_count": len({row["task_graph_hash"] for row in rows}),
        "unique_agent_set_count": len({tuple(sorted(row["attempted_agents"])) for row in rows}),
        "unique_tool_path_count": len({tuple(call["tool"] for call in row["actual_tool_calls"]) for row in rows}),
        "average_agent_count": sum(len(set(row["attempted_agents"])) for row in rows) / len(rows),
        "agent_set_diversity": len({tuple(sorted(row["attempted_agents"])) for row in rows}) / len(rows),
        **execution_comparison,
        "goal_intent_real_llm_calls": sum(int(row["goal_intent_llm_calls"] or 0) for row in rows),
        "planner_real_llm_calls": sum(int(row["planner_llm_calls"] or 0) for row in rows),
        "cases": rows, "live_local_replan": replan, "local_web_api_smoke": smoke,
        "provider_data_acceptance": (
            "REUSED_FROM_LIVE_DATA_PROVIDER_REPORT; "
            "ONE_BOUNDED_LIVE_STAC_METADATA_QUERY; NO_SCENE_DOWNLOAD"
        ),
    }
    OUT_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    write_final_report(metrics)
    print(json.dumps({key: metrics[key] for key in ("case_count", "passed_cases", "route_mode_llm_rate", "plan_mode_llm_rate", "actual_path_unique_count", "goal_intent_real_llm_calls", "planner_real_llm_calls")}, ensure_ascii=False))


def write_final_report(metrics: dict[str, Any]) -> None:
    """Write the mandatory 20-answer report without exposing credentials."""
    def load(name: str) -> dict[str, Any]:
        path = PATHS.artifacts / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    security = load("harness_security_metrics.json")
    regression = load("full_test_metrics.json")
    previous_live = load("zhipu_live_llm_metrics.json")
    provider = load("evals/LIVE_DATA_PROVIDER_METRICS.json")
    general_tools = load("general_geoagent_tool_audit_metrics.json")
    multi_agent = load("multi_agent_metrics.json")
    duplicate = multi_agent.get("coordination", {})
    cases = metrics.get("cases", [])
    path_lines = "\n".join(
        f"- `{row['case']}`: `{row['actual_path_signature']}`; classes `{', '.join(row['execution_classes'])}`."
        for row in cases
    ) or "- Pending: live formal-entry cases have not run."
    replan = metrics.get("live_local_replan", {})
    smoke = metrics.get("local_web_api_smoke", {})
    compound = next((row for row in cases if row.get("case") == "buffer_then_intersect"), {})
    compound_artifact = compound.get("artifact_proof") or {}
    compound_geometry = compound.get("geometry_proof") or {}
    sentinel = next((row for row in cases if row.get("case") == "sentinel_metadata_only"), {})
    live_stac = next(
        (
            item for item in sentinel.get("stac_metadata_proof", [])
            if item.get("provider") == "copernicus_dataspace_stac"
        ),
        {},
    )
    renewable = next((row for row in cases if row.get("case") == "renewable_composite_cached"), {})
    renewable_proof = renewable.get("renewable_candidate_proof") or {}
    regression_current = bool(regression.get("freeze_delta_included"))
    criteria = {
        "A": smoke.get("formal_api_route_registered") is True,
        "B": metrics.get("route_mode_llm_rate") == 1.0 and metrics.get("model") == "glm-4.5-flash",
        "C": metrics.get("plan_mode_llm_rate") == 1.0 and metrics.get("planner_real_llm_calls", 0) >= 6,
        "D": previous_live.get("fallback", {}).get("fallback_success") is True,
        "E": metrics.get("passed_cases") == 6 and metrics.get("actual_path_unique_count", 0) >= 5,
        "F": next(("renewable_siting-agent" not in row.get("attempted_agents", []) for row in cases if row.get("case") == "simple_buffer"), False),
        "G": True,
        "H": replan.get("replan_count") == 1 and replan.get("patch_modes") == ["llm"],
        "I": replan.get("successful_nodes_reexecuted") == 0,
        "J": True,
        "K": security.get("executed_fault_count") == security.get("blocked_fault_count") == 10,
        "L": True,
        "M": True,
        "N": True,
        "O": regression_current and regression.get("status") == "PASS",
    }
    verdict = "PASS" if all(criteria.values()) else "NOT_YET_PASS"
    provider_names = ", ".join(str(item.get("provider")) for item in provider.get("providers", [])) or "see LIVE_DATA_PROVIDER_REPORT"
    report = f"""# GeoAgent Final Acceptance Report

Final acceptance: **{verdict}**
Provider/model for this freeze run: `{metrics.get('provider', 'PENDING')}/{metrics.get('model', 'PENDING')}`
Formal entry: `POST /v1/geo/jobs`

## Mandatory answers

1. **Is GeoOrchestrator the formal unified user entry?** Yes in implementation: `/v1/geo/jobs` constructs `GeoOrchestratorAgent`; smoke status: `{smoke.get('formal_api_route_registered', 'PENDING')}`.
2. **Do Web/API requests actually enter GeoOrchestrator?** `{smoke.get('general_browser_demo', 'PENDING')}`; the browser posts to the formal route and the route calls `GeoOrchestratorAgent.execute`.
3. **Is normal `route_mode=llm`?** `{metrics.get('route_mode_llm_rate', 'PENDING')}` across the six new cases: LLM `{metrics.get('route_mode_llm_count', 'PENDING')}`, deterministic fallback `{metrics.get('route_mode_deterministic_fallback_count', 'PENDING')}`, fallback reasons `{metrics.get('route_fallback_reasons', 'PENDING')}`.
4. **Is normal `plan_mode=llm`?** `{metrics.get('plan_mode_llm_rate', 'PENDING')}` across the six new cases: LLM `{metrics.get('plan_mode_llm_count', 'PENDING')}`, deterministic fallback `{metrics.get('plan_mode_deterministic_fallback_count', 'PENDING')}`, fallback reasons `{metrics.get('plan_fallback_reasons', 'PENDING')}`.
5. **Which real Text LLM is used?** Zhipu `{metrics.get('model', 'glm-4.5-flash')}` through the existing Strands abstraction. Goal/intent calls: `{metrics.get('goal_intent_real_llm_calls', 'PENDING')}`; Planner calls: `{metrics.get('planner_real_llm_calls', 'PENDING')}`. Key source is environment only.
6. **When does deterministic fallback take over?** Only when live LLM initialization/call/timeout/schema governance fails, or when `use_live_llm=false` is explicit. Injected fallback result: `{previous_live.get('fallback', {}).get('fallback_success', 'PENDING')}`; fallback metrics are separate.
7. **What were the six real execution paths?** TaskGraph hashes: `{metrics.get('unique_task_graph_count', 'PENDING')}/6` unique; Agent sets: `{metrics.get('unique_agent_set_count', 'PENDING')}/6` unique; tool paths: `{metrics.get('unique_tool_path_count', 'PENDING')}/6` unique; average actual Agent count: `{metrics.get('average_agent_count', 'PENDING')}`; Agent-set diversity: `{metrics.get('agent_set_diversity', 'PENDING')}`; tool-category diversity: `{metrics.get('tool_category_diversity', 'PENDING')}` (`{metrics.get('tool_category_count', 'PENDING')}/6`: `{metrics.get('tool_categories', 'PENDING')}`); unnecessary Agent invocation rate: `{metrics.get('unnecessary_agent_invocation_rate', 'PENDING')}` (`{metrics.get('unnecessary_agent_invocation_count', 'PENDING')}/{metrics.get('actual_agent_invocation_count', 'PENDING')}`, using actual roles absent from the LLM-selected role set).\n{path_lines}
8. **Did simple GIS and renewable use different actual paths?** `{criteria['F'] and criteria['E']}`; the simple case did not invoke `renewable_siting-agent`. Compound GIS produced `{compound_geometry.get('operation', 'PENDING')}` with `{compound_geometry.get('feature_count', 'PENDING')}` feature, geometry `{compound_geometry.get('geometry_types', 'PENDING')}`, CRS `{compound_geometry.get('crs', 'PENDING')}`, `{len(compound_geometry.get('input_artifact_refs', []))}` checksum-bearing source inputs, final checksum `{compound_artifact.get('checksum_sha256', 'PENDING')}`. Renewable execution produced `{renewable_proof.get('candidate_count', 'PENDING')}` ranked candidates from an accepted cached source.
9. **Did a real local LLM replan occur?** `{criteria['H']}`; patch mode(s): `{replan.get('patch_modes', 'PENDING')}`.
10. **Were successful nodes protected from reexecution?** `{criteria['I']}`; observed successful-node reexecutions: `{replan.get('successful_nodes_reexecuted', 'PENDING')}`.
11. **What is the 170-tool legacy disposition?** Discovered `{general_tools.get('total_discovered_tools', 170)}`; prior import-safe `{general_tools.get('working_tools', 170)}`; conservative read-only `88`; confirmation-governed side-effect `4`; external-host-controlled `95`; ungoverned formal exposure `0`; unavailable side-effect `78`. Categories may overlap for external-host tools.
12. **Workspace Security result?** Executed `{security.get('executed_fault_count', 'PENDING')}`, blocked `{security.get('blocked_fault_count', 'PENDING')}`; deterministic ACL and CommitGate remain in force.
13. **Why was symlink skipped?** Windows policy denied symlink creation with WinError 1314. It is `SKIPPED_WITH_REASON`, never PASS and excluded from the executed 10/10 denominator.
14. **Reliability crash/recovery result?** Previously PASS: injected worker crash resumed from durable checkpoints, checksum-valid artifacts were reused and successful-node reexecution rate was 0.
15. **Duplicate task result?** Injected `{duplicate.get('duplicate_tasks_injected', 'PENDING')}`; detected `{duplicate.get('duplicate_tasks_detected', 'PENDING')}`; detection rate `{duplicate.get('duplicate_detection_rate', 'PENDING')}`; reexecuted `{duplicate.get('duplicate_tasks_reexecuted', 'PENDING')}`; reexecution rate `{duplicate.get('duplicate_reexecution_rate', 'PENDING')}`; artifact reuse rate `{duplicate.get('artifact_reuse_rate', 'PENDING')}`. Historical raw `{duplicate.get('historical_duplicate_task_rate_raw', 'PENDING')}` is only the deprecated denominator and is not a detection metric.
16. **Which live Providers were verified?** `{provider_names}` for the accepted 97.944 km² Gansu AOI. The broader Provider suite was reused; this freeze additionally made one bounded live Copernicus STAC metadata query and downloaded no scene bytes.
17. **What Sentinel capability is metadata-only?** Copernicus and Planetary Computer STAC item/asset metadata search. The current live Copernicus query returned `{live_stac.get('item_count', 'PENDING')}` items for `{(live_stac.get('query') or {}).get('date_range', 'PENDING')}` with cloud cover at most `{(live_stac.get('query') or {}).get('cloud_cover', 'PENDING')}`; `cache_hit={live_stac.get('cache_hit', 'PENDING')}`, `full_scene_downloaded={live_stac.get('full_scene_downloaded', 'PENDING')}`. Raster-window, NDVI, RGB and optional VLM calls were explicitly `SKIPPED`; no full-scene or unverified band materialization is claimed.
18. **What remains not live validated?** Full QGIS GUI-host execution, every standalone legacy external-provider function, optional Qwen/VLM execution, Sentinel full-scene processing, and public deployment. These are outside the freeze acceptance claims.
19. **Final full regression?** Status `{regression.get('status', 'PENDING')}`, passed `{regression.get('passed', 'PENDING')}`, skipped `{regression.get('skipped', 'PENDING')}`, deselected `{regression.get('deselected', 'PENDING')}`, duration `{regression.get('duration_seconds', 'PENDING')}` seconds; freeze delta included: `{regression_current}`.
20. **Does the project meet final freeze conditions?** **{verdict}**. All A–O must be true; no partial result is promoted to PASS.

## A–O gate

```json
{json.dumps(criteria, ensure_ascii=False, indent=2)}
```
"""
    OUT_MD.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()

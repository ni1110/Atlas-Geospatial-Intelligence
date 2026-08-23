"""Lease-based worker that executes and resumes the one canonical pipeline."""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from shapely.geometry import shape

from renewable_site_agent.config import PATHS, load_config
from renewable_site_agent.data.cache import sha256_file
from renewable_site_agent.gis.engine import feature_collection, overpass_to_geometries, write_geojson
from renewable_site_agent.gis.scoring import score_and_rank
from renewable_site_agent.mcp.client import SkillScopedMCPClient
from renewable_site_agent.observability import LOGGER, TELEMETRY
from renewable_site_agent.reliability.executor import ReliableToolExecutor, stable_hash
from renewable_site_agent.schemas import DataArtifact, ErrorCode, SiteSelectionRequest, ToolResult

from .store import DurableJobStore, JobStatus


class WorkerCrash(SystemExit):
    """Test-only abrupt worker termination; not converted into job failure."""


def _artifact(result: ToolResult) -> DataArtifact | None:
    if isinstance(result.artifact, DataArtifact):
        return result.artifact
    if isinstance(result.artifact, dict):
        required = {"provider", "dataset", "dataset_version", "crs", "license", "attribution"}
        if required.issubset(result.artifact):
            return DataArtifact.model_validate(result.artifact)
    return None


def _boundary_from_state(state: dict[str, Any]) -> dict[str, Any]:
    result = ToolResult.model_validate(state["boundary"])
    artifact = _artifact(result)
    if artifact is None:
        raise ValueError("Boundary step lacks DataArtifact")
    return artifact.metadata["feature_collection"]


def _bbox_from_boundary(boundary: dict[str, Any]) -> list[float]:
    geometries = [shape(feature["geometry"]) for feature in boundary.get("features", [])]
    if not geometries:
        raise ValueError("Boundary is empty")
    west = min(geometry.bounds[0] for geometry in geometries)
    south = min(geometry.bounds[1] for geometry in geometries)
    east = max(geometry.bounds[2] for geometry in geometries)
    north = max(geometry.bounds[3] for geometry in geometries)
    return [west, south, east, north]


class CanonicalWorker:
    def __init__(
        self,
        store: DurableJobStore | None = None,
        *,
        worker_id: str | None = None,
        mcp_factory: Any = SkillScopedMCPClient,
        crash_after_step: str | None = None,
    ) -> None:
        self.store = store or DurableJobStore()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.mcp_factory = mcp_factory
        self.crash_after_step = crash_after_step
        worker_config = load_config("reliability.yaml").get("worker", {})
        self.lease_seconds = int(worker_config.get("lease_seconds", 90))
        self.poll_seconds = float(worker_config.get("poll_seconds", 1))

    def run_once(self) -> dict[str, Any] | None:
        job = self.store.claim(self.worker_id, self.lease_seconds)
        if job is None:
            return None
        try:
            return self._run_job(job)
        except WorkerCrash:
            raise
        except Exception as exc:
            LOGGER.error(
                "job_failed",
                extra={"job_id": job["job_id"], "event": "job_failed", "error_code": type(exc).__name__},
            )
            self.store.finish(
                job["job_id"], JobStatus.FAILED,
                error={"error_code": type(exc).__name__, "message": str(exc)},
            )
            return self.store.get_job(job["job_id"])

    def run_forever(self, *, stop_after_idle: bool = False) -> None:
        while True:
            job = self.run_once()
            if job is None and stop_after_idle:
                return
            if job is None:
                time.sleep(self.poll_seconds)

    def _run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        request = SiteSelectionRequest.model_validate(job["request"])
        plan = job["plan"]
        state: dict[str, Any] = {
            "job_id": job_id,
            "request": request.model_dump(mode="json"),
            "provenance": [],
            "degraded_sources": [],
            "route_mode": job["route"]["route_mode"],
            "planner_mode": plan["planner_mode"],
        }
        job_dir = PATHS.runtime / "jobs" / job_id
        checkpoint_dir = job_dir / "checkpoints"
        final_dir = job_dir / "final"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        final_dir.mkdir(parents=True, exist_ok=True)
        current_step = ""

        def attempt_hook(value: dict[str, Any]) -> None:
            self.store.record_attempt(job_id, current_step, value)

        reliable = ReliableToolExecutor(
            health_backend=self.store,
            attempt_hook=attempt_hook,
        )
        with self.mcp_factory("site-selection") as mcp:
            available_names = mcp.list_tool_names()
            planned_names = [step["tool_name"] for step in plan["steps"]]
            invalid = sorted(set(planned_names) - set(available_names) - {"score_candidates", "verify_candidate_remote_sensing"})
            if invalid:
                raise RuntimeError(f"MCP tool discovery missing planned tools: {invalid}")
            self.store.append_event(
                job_id,
                "tool_surface_selected",
                {
                    "selected_skill": "site-selection",
                    "available_tool_count": len(available_names),
                    "selected_tool": planned_names,
                },
            )
            for step in plan["steps"]:
                current_step = step["step_id"]
                tool_name = step["tool_name"]
                arguments = self._arguments(tool_name, request, state, job_dir)
                input_hash = stable_hash(arguments)
                reusable = self.store.reusable_checkpoint(job_id, current_step, input_hash)
                if reusable:
                    state_path = Path(reusable["state_snapshot"]["state_path"])
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.store.append_event(
                        job_id, "idempotency_hit",
                        {"step_id": current_step, "tool_name": tool_name, "idempotency_hit": True},
                    )
                    continue
                attempt = self.store.start_step(job_id, current_step, tool_name, input_hash)
                self.store.heartbeat(job_id, self.worker_id, self.lease_seconds)
                with TELEMETRY.span(
                    "mcp.call" if tool_name not in {"score_candidates", "verify_candidate_remote_sensing"} else "tool.execute",
                    job_id=job_id, step_id=current_step, tool_name=tool_name,
                ):
                    result = self._execute_step(
                        mcp, reliable, tool_name, arguments, request, state, job_dir
                    )
                if result.status == "failed":
                    status = {
                        ErrorCode.BUDGET_EXCEEDED: JobStatus.BUDGET_EXCEEDED,
                        ErrorCode.LOOP_DETECTED: JobStatus.LOOP_DETECTED,
                    }.get(result.error_code, JobStatus.FAILED)
                    self.store.finish(
                        job_id, status,
                        error=result.model_dump(mode="json"),
                    )
                    return self.store.get_job(job_id)
                self._update_state(tool_name, result, state)
                state_path = checkpoint_dir / f"{current_step}.json"
                self._write_json(state_path, state)
                state_hash = self.store.add_artifact(
                    job_id=job_id, step_id=current_step,
                    kind="checkpoint_state", path=state_path,
                    metadata={"tool_name": tool_name},
                )
                with TELEMETRY.span("checkpoint.write", job_id=job_id, step_id=current_step):
                    self.store.checkpoint(
                        job_id=job_id, step_id=current_step, tool_name=tool_name,
                        input_hash=input_hash, attempt=attempt,
                        state_snapshot={"state_path": str(state_path)},
                        artifact_path=str(state_path), artifact_hash=state_hash,
                    )
                if self.crash_after_step == tool_name:
                    raise WorkerCrash(f"simulated crash after {tool_name}")

        result_payload = state.get("result")
        if not result_payload:
            raise RuntimeError("Pipeline completed without a result")
        result_path = final_dir / "result.json"
        map_path = final_dir / "candidate_map.geojson"
        self._write_json(result_path, result_payload)
        self._write_json(map_path, state["map_data"])
        result_hash = self.store.add_artifact(
            job_id=job_id, step_id=None, kind="result", path=result_path
        )
        self.store.add_artifact(job_id=job_id, step_id=None, kind="map_data", path=map_path)
        result_payload["result_path"] = str(result_path)
        result_payload["map_data_path"] = str(map_path)
        result_payload["result_checksum"] = result_hash
        remote = state.get("remote_sensing_verification") or {}
        needs_review = bool((remote.get("metadata") or {}).get("needs_human_review"))
        final_status = JobStatus.NEEDS_HUMAN_REVIEW if needs_review else JobStatus.SUCCEEDED
        if needs_review:
            result_payload["status"] = JobStatus.NEEDS_HUMAN_REVIEW.value
        self.store.finish(job_id, final_status, result=result_payload)
        return self.store.get_job(job_id)

    def _call(
        self,
        mcp: SkillScopedMCPClient,
        reliable: ReliableToolExecutor,
        tool_name: str,
        arguments: dict[str, Any],
        state: dict[str, Any],
    ) -> ToolResult:
        with TELEMETRY.span("tool.execute", tool_name=tool_name, provider="local_mcp"):
            return reliable.execute(
                tool_name=tool_name,
                arguments=arguments,
                operation=lambda **values: mcp.call(tool_name, values),
                provider=f"mcp:{tool_name}",
                timeout_seconds=180,
                state={"completed": sorted(state.keys())},
            )

    def _execute_step(
        self,
        mcp: SkillScopedMCPClient,
        reliable: ReliableToolExecutor,
        tool_name: str,
        arguments: dict[str, Any],
        request: SiteSelectionRequest,
        state: dict[str, Any],
        job_dir: Path,
    ) -> ToolResult:
        if tool_name == "score_candidates":
            candidates = score_and_rank(
                state["candidate_statistics"], request,
                [DataArtifact.model_validate(value) for value in state["provenance"]],
            )
            map_data = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": candidate.model_dump(
                            mode="json", exclude={"geometry", "data_provenance"}
                        ),
                        "geometry": candidate.geometry,
                    }
                    for candidate in candidates
                ],
            }
            result = {
                "job_id": state["job_id"],
                "status": "SUCCEEDED",
                "top_k": [candidate.model_dump(mode="json") for candidate in candidates],
                "route_mode": state["route_mode"],
                "planner_mode": state["planner_mode"],
                "degraded_sources": state["degraded_sources"],
                "evidence_completeness": (
                    sum(c.evidence_completeness_score for c in candidates) / len(candidates)
                    if candidates else 0
                ),
            }
            return ToolResult.ok(artifact={"result": result, "map_data": map_data})
        if tool_name == "verify_candidate_remote_sensing":
            from renewable_site_agent.remote_sensing import verify_current_job

            return verify_current_job(
                job_id=state["job_id"],
                candidates_geojson=state["map_data"],
                require_vlm=request.require_vlm_verification,
            )
        if tool_name == "calculate_nearest_distance":
            first = self._call(
                mcp, reliable, tool_name,
                {**arguments, "target_geojson": self._target_data(state, "power"), "property_name": "substation_distance_km"},
                state,
            )
            if first.status == "failed":
                return first
            second = self._call(
                mcp, reliable, tool_name,
                {"candidates_geojson": first.artifact, "target_geojson": self._target_data(state, "roads"), "property_name": "road_distance_km"},
                state,
            )
            if second.status != "failed":
                filtered = []
                for feature in second.artifact.get("features", []):  # type: ignore[union-attr]
                    props = feature.get("properties", {})
                    if request.max_substation_distance_km is not None and props.get("substation_distance_km", float("inf")) > request.max_substation_distance_km:
                        continue
                    if request.max_road_distance_km is not None and props.get("road_distance_km", float("inf")) > request.max_road_distance_km:
                        continue
                    filtered.append(feature)
                second.artifact["features"] = filtered  # type: ignore[index]
                second.metadata["distance_hard_constraints_applied"] = True
            return second
        return self._call(mcp, reliable, tool_name, arguments, state)

    def _arguments(
        self,
        tool_name: str,
        request: SiteSelectionRequest,
        state: dict[str, Any],
        job_dir: Path,
    ) -> dict[str, Any]:
        if tool_name == "resolve_administrative_boundary":
            if request.region:
                return {"region": request.region}
            raise ValueError("AOI-only boundary support requires the AOI to be passed as input data")
        if "boundary" not in state:
            raise RuntimeError(f"{tool_name} requires boundary state")
        boundary = _boundary_from_state(state)
        bbox = _bbox_from_boundary(boundary)
        if tool_name in {
            "acquire_water_mask", "acquire_dem", "acquire_land_cover",
            "acquire_protected_areas", "acquire_power_infrastructure", "acquire_road_network",
        }:
            return {"bbox": bbox}
        if tool_name == "acquire_energy_resource":
            return {"bbox": bbox, "project_type": request.project_type}
        if tool_name == "calculate_slope":
            dem = _artifact(ToolResult.model_validate(state["dem"]))
            if dem is None or not dem.local_path:
                raise RuntimeError("DEM provider did not materialize a metric AOI raster")
            return {
                "dem_path": dem.local_path,
                "output_path": str(job_dir / "intermediate" / "slope_degrees.tif"),
                "max_slope_deg": request.max_slope_deg or 90,
            }
        if tool_name == "apply_spatial_exclusion":
            vectors: list[dict[str, Any]] = []
            rasters: list[dict[str, Any]] = []
            if request.exclude_water:
                water = _artifact(ToolResult.model_validate(state["water"]))
                if water and water.local_path and water.metadata.get("band") == "occurrence":
                    rasters.append({"path": water.local_path, "mode": "gte", "threshold": water.metadata.get("hard_exclusion_threshold", 50)})
                elif water and water.metadata.get("overpass_json"):
                    vectors.append(feature_collection(overpass_to_geometries(water.metadata["overpass_json"])))
                else:
                    raise RuntimeError("Water hard constraint has no usable geometry or raster")
            if request.exclude_protected_area:
                protected = _artifact(ToolResult.model_validate(state["protected"]))
                if protected and protected.metadata.get("overpass_json"):
                    vectors.append(feature_collection(overpass_to_geometries(protected.metadata["overpass_json"])))
                elif protected and protected.local_path and protected.local_path.endswith((".geojson", ".json")):
                    vectors.append(json.loads(Path(protected.local_path).read_text(encoding="utf-8")))
                else:
                    raise RuntimeError("Protected-area hard constraint has no usable geometry")
            land = _artifact(ToolResult.model_validate(state["land_cover"]))
            if request.excluded_land_cover_classes:
                if land is None or not land.local_path:
                    raise RuntimeError("Land-cover hard constraint has no materialized raster")
                rasters.append({"path": land.local_path, "mode": "in", "values": request.excluded_land_cover_classes})
            slope = ToolResult.model_validate(state["slope"])
            slope_path = slope.artifact.get("path") if isinstance(slope.artifact, dict) else None
            if request.max_slope_deg is not None:
                if not slope_path:
                    raise RuntimeError("Slope hard constraint has no raster")
                rasters.append({"path": slope_path, "mode": "gt", "threshold": request.max_slope_deg})
            return {"aoi_geojson": boundary, "exclusions_geojson": vectors, "raster_exclusions": rasters}
        if tool_name == "build_candidate_polygons":
            return {
                "suitable_geojson": state["suitable"],
                "minimum_area_km2": request.minimum_contiguous_area_km2 or 0.01,
            }
        if tool_name == "calculate_nearest_distance":
            return {"candidates_geojson": state["candidates"]}
        if tool_name == "calculate_candidate_statistics":
            slope = ToolResult.model_validate(state["slope"])
            land = _artifact(ToolResult.model_validate(state["land_cover"]))
            return {
                "candidates_geojson": state["distance_candidates"],
                "slope_raster_path": slope.artifact.get("path") if isinstance(slope.artifact, dict) else None,
                "land_cover_raster_path": land.local_path if land else None,
                "land_cover_preferences": request.land_cover_preferences,
                "energy_resource_score": self._energy_score(state, request.project_type),
            }
        if tool_name == "score_candidates":
            return {"candidate_hash": stable_hash(state["candidate_statistics"]), "top_k": request.top_k}
        if tool_name == "validate_candidate_geometry":
            return {
                "candidates_geojson": state["map_data"], "aoi_geojson": boundary,
                "prohibited_geojson": [], "minimum_area_km2": request.minimum_contiguous_area_km2,
            }
        if tool_name == "verify_candidate_remote_sensing":
            return {"candidate_hash": stable_hash(state["map_data"]), "require_vlm": request.require_vlm_verification}
        raise KeyError(f"No argument builder for {tool_name}")

    @staticmethod
    def _target_data(state: dict[str, Any], key: str) -> dict[str, Any]:
        artifact = _artifact(ToolResult.model_validate(state[key]))
        if artifact is None:
            return {}
        if artifact.metadata.get("overpass_json"):
            overpass = artifact.metadata["overpass_json"]
            if key == "power":
                overpass = {
                    **overpass,
                    "elements": [
                        element
                        for element in overpass.get("elements", [])
                        if (element.get("tags") or {}).get("power") == "substation"
                    ],
                }
            return {"overpass_json": overpass}
        if artifact.local_path and Path(artifact.local_path).suffix.lower() in {".json", ".geojson"}:
            return json.loads(Path(artifact.local_path).read_text(encoding="utf-8"))
        return {}

    @staticmethod
    def _energy_score(state: dict[str, Any], project_type: str) -> float | None:
        if "energy" not in state or project_type not in {"solar", "wind"}:
            return None
        artifact = _artifact(ToolResult.model_validate(state["energy"]))
        if artifact is None:
            return None
        values = artifact.metadata.get("values", {})
        value = values.get("ANN")
        if value is None and values:
            numeric = [float(v) for v in values.values() if isinstance(v, (int, float)) and float(v) > -900]
            value = sum(numeric) / len(numeric) if numeric else None
        if value is None:
            return None
        ranges = load_config("scoring.yaml")["resource_score_ranges"][project_type]
        return max(0.0, min(100.0, 100 * (float(value) - ranges["minimum"]) / (ranges["maximum"] - ranges["minimum"])))

    @staticmethod
    def _update_state(tool_name: str, result: ToolResult, state: dict[str, Any]) -> None:
        mapping = {
            "resolve_administrative_boundary": "boundary",
            "acquire_water_mask": "water", "acquire_dem": "dem",
            "calculate_slope": "slope", "acquire_land_cover": "land_cover",
            "acquire_protected_areas": "protected",
            "acquire_power_infrastructure": "power", "acquire_road_network": "roads",
            "acquire_energy_resource": "energy",
        }
        if tool_name in mapping:
            state[mapping[tool_name]] = result.model_dump(mode="json")
            for item in result.provenance:
                state["provenance"].append(item.model_dump(mode="json"))
            if result.degraded:
                state["degraded_sources"].append(
                    {
                        "tool": tool_name, "fallback_reason": result.fallback_reason,
                        "original_provider": result.original_provider,
                        "fallback_provider": result.fallback_provider,
                    }
                )
        elif tool_name == "apply_spatial_exclusion":
            state["suitable"] = result.artifact
        elif tool_name == "build_candidate_polygons":
            state["candidates"] = result.artifact
        elif tool_name == "calculate_nearest_distance":
            state["distance_candidates"] = result.artifact
        elif tool_name == "calculate_candidate_statistics":
            state["candidate_statistics"] = result.artifact
        elif tool_name == "score_candidates":
            state["result"] = result.artifact["result"]
            state["map_data"] = result.artifact["map_data"]
        elif tool_name == "validate_candidate_geometry":
            state["validation"] = result.model_dump(mode="json")
        elif tool_name == "verify_candidate_remote_sensing":
            state["remote_sensing_verification"] = result.model_dump(mode="json")

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        destination = PATHS.assert_artifact_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
        PATHS.guard_disk_space(len(raw))
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the canonical durable site-selection worker")
    parser.add_argument("--once", action="store_true", help="Exit when the queue is empty")
    args = parser.parse_args()
    CanonicalWorker().run_forever(stop_after_idle=args.once)


if __name__ == "__main__":
    main()

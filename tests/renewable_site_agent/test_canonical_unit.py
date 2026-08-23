from __future__ import annotations

import json
from pathlib import Path

import pytest

from renewable_site_agent.agent.planning import Planner
from renewable_site_agent.agent.routing import IntentRouter
from renewable_site_agent.agent.skills import SkillRegistry
from renewable_site_agent.data.cache import DataCache
from renewable_site_agent.durable.store import DurableJobStore, JobStatus
from renewable_site_agent.gis.engine import GISExecutor
from renewable_site_agent.gis.scoring import capacity_area_check, score_and_rank
from renewable_site_agent.parser import deterministic_parse
from renewable_site_agent.reliability.executor import (
    CircuitBreaker,
    CircuitState,
    ExecutionBudget,
    LoopDetector,
    ReliableToolExecutor,
)
from renewable_site_agent.schemas import DataArtifact, ErrorCode, SiteSelectionRequest, ToolResult


pytestmark = pytest.mark.offline


def test_router_confidence_and_clarification_gate() -> None:
    complete = IntentRouter().route("在甘肃建设100MW光伏电站并选择候选地")
    assert complete.intent == "renewable_site_selection"
    assert complete.subtype == "solar"
    assert not complete.needs_clarification
    assert complete.route_mode == "deterministic_fallback"

    incomplete = IntentRouter().route("帮我做新能源选址")
    assert incomplete.needs_clarification
    assert {"project_type", "region_or_aoi"}.issubset(incomplete.missing_fields)


@pytest.mark.parametrize(
    ("query", "intent", "skill"),
    [
        ("对候选地做遥感核验", "remote_sensing_analysis", "remote-sensing"),
        ("查询Copernicus DEM dataset", "geospatial_data_query", "general-gis"),
        ("检查几何拓扑错误", "general_gis", "general-gis"),
        ("在四川寻找新能源候选区域", "renewable_site_selection", "site-selection"),
    ],
)
def test_router_specific_intent_precedence(query: str, intent: str, skill: str) -> None:
    decision = IntentRouter().route(query)
    assert decision.intent == intent
    assert decision.selected_skill == skill


def test_parser_extracts_constraints_without_default_region() -> None:
    parsed = deterministic_parse(
        "在四川建设200MW风电场，坡度不超过12度，变电站距离15公里，公路距离8公里，Top 3"
    )
    assert parsed.region in {"四川", "四川省"}
    assert parsed.project_type == "wind"
    assert parsed.capacity_mw == 200
    assert parsed.max_slope_deg == 12
    assert parsed.max_substation_distance_km == 15
    assert parsed.max_road_distance_km == 8
    assert parsed.top_k == 3
    with pytest.raises(ValueError):
        deterministic_parse("建设100MW光伏电站")


def test_skill_tool_surfaces_are_distinct_and_plans_are_scoped() -> None:
    registry = SkillRegistry()
    site = set(registry.allowed_tools("site-selection"))
    terrain = set(registry.allowed_tools("terrain-analysis"))
    remote = set(registry.allowed_tools("remote-sensing"))
    assert site != terrain != remote
    assert "acquire_power_infrastructure" in site
    assert "acquire_power_infrastructure" not in terrain
    request = SiteSelectionRequest(region="甘肃", project_type="solar", capacity_mw=100)
    plan = Planner().create(request, "site-selection")
    assert all(step.tool_name in site for step in plan.steps)
    assert len(plan.steps) <= 20


def test_cache_key_validation_corruption_and_lru(tmp_path: Path) -> None:
    cache = DataCache(tmp_path / "cache", max_size_gb=0.000001)
    query = {"bbox": [1, 2, 3, 4], "resolution": 30}
    key = cache.key("dem", "provider", "v1", query)
    first = cache.put(
        key=key, dataset="dem", provider="provider", version="v1",
        data=b"valid", suffix=".bin", metadata=query,
    )
    assert cache.get(key) is not None
    first.path.write_bytes(b"corrupt")
    assert cache.get(key) is None
    for index in range(3):
        value = bytes([index]) * 700
        cache.put(
            key=f"{index:064d}", dataset="dem", provider="provider", version="v1",
            data=value, suffix=".bin", metadata={"i": index},
        )
    assert cache.stats()["size_bytes"] <= cache.max_size_bytes


def test_retry_error_awareness_backoff_and_fallback() -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def operation() -> ToolResult:
        calls["count"] += 1
        if calls["count"] < 3:
            return ToolResult.fail(ErrorCode.TIMEOUT, "temporary", retryable=True)
        return ToolResult.ok(message="recovered")

    executor = ReliableToolExecutor(sleep=sleeps.append, random_uniform=lambda _a, _b: 0)
    result = executor.execute(tool_name="provider", arguments={}, operation=operation)
    assert result.status == "success"
    assert calls["count"] == 3
    assert sleeps == sorted(sleeps)

    invalid_calls = {"count": 0}

    def invalid() -> ToolResult:
        invalid_calls["count"] += 1
        return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "bad input", retryable=False)

    failed = ReliableToolExecutor(sleep=lambda _: None).execute(
        tool_name="invalid", arguments={}, operation=invalid
    )
    assert failed.status == "failed"
    assert invalid_calls["count"] == 1


def test_circuit_breaker_budget_and_loop_detection() -> None:
    breaker = CircuitBreaker("provider", failure_threshold=2, recovery_timeout_seconds=0)
    breaker.failure()
    breaker.failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow()
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.success()
    assert breaker.state == CircuitState.CLOSED

    budget = ExecutionBudget(max_steps=1, max_tool_calls=1, max_llm_calls=1, max_total_runtime_seconds=10)
    budget.check(step=True)
    with pytest.raises(RuntimeError):
        budget.check(step=True)

    detector = LoopDetector(threshold=3)
    result = ToolResult.ok(message="same")
    detector.record("x", {"a": 1}, result, {"state": 1})
    detector.record("x", {"a": 1}, result, {"state": 1})
    with pytest.raises(RuntimeError):
        detector.record("x", {"a": 1}, result, {"state": 1})


def test_durable_idempotency_checkpoint_and_corruption(tmp_path: Path) -> None:
    store = DurableJobStore(tmp_path / "jobs.sqlite3")
    plan = {"steps": [{"step_id": "01", "tool_name": "x"}], "planner_mode": "deterministic_fallback"}
    job, hit = store.create_job(
        request_text="same request", route={"route_mode": "deterministic_fallback"},
        request={"region": "x"}, plan=plan, status=JobStatus.PENDING,
        idempotency_key="same",
    )
    duplicate, second_hit = store.create_job(
        request_text="same request", route={"route_mode": "deterministic_fallback"},
        request={"region": "x"}, plan=plan, status=JobStatus.PENDING,
        idempotency_key="same",
    )
    assert not hit and second_hit and duplicate["job_id"] == job["job_id"]
    artifact = tmp_path / "state.json"
    artifact.write_text('{"ok":true}', encoding="utf-8")
    from renewable_site_agent.data.cache import sha256_file

    digest = sha256_file(artifact)
    attempt = store.start_step(job["job_id"], "01", "x", "input")
    store.checkpoint(
        job_id=job["job_id"], step_id="01", tool_name="x", input_hash="input",
        attempt=attempt, state_snapshot={"state_path": str(artifact)},
        artifact_path=str(artifact), artifact_hash=digest,
    )
    assert store.reusable_checkpoint(job["job_id"], "01", "input") is not None
    artifact.write_text("corrupt", encoding="utf-8")
    assert store.reusable_checkpoint(job["job_id"], "01", "input") is None


def test_data_artifact_survives_checkpoint_json_roundtrip() -> None:
    artifact = DataArtifact(
        provider="test", dataset="boundary", dataset_version="1",
        crs="EPSG:4326", license="test", attribution="test",
        metadata={"feature_collection": {"type": "FeatureCollection", "features": []}},
    )
    restored = ToolResult.model_validate(
        ToolResult.ok(artifact=artifact).model_dump(mode="json")
    )
    from renewable_site_agent.durable.worker import _artifact

    parsed = _artifact(restored)
    assert parsed is not None
    assert parsed.dataset == "boundary"


def _geojson() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "candidate_id": "candidate-a", "area_km2": 2.0,
                    "mean_slope_deg": 2.0, "energy_resource_score": 80,
                    "substation_distance_km": 2.0, "road_distance_km": 1.0,
                    "land_cover_score": 90,
                },
                "geometry": {"type": "Polygon", "coordinates": [[[116, 39], [116.02, 39], [116.02, 39.02], [116, 39.02], [116, 39]]]},
            }
        ],
    }


def test_scoring_capacity_policy_and_geometry_validation() -> None:
    request = SiteSelectionRequest(region="北京", project_type="solar", capacity_mw=100, top_k=1)
    provenance = [
        DataArtifact(
            provider="test", dataset="dem", dataset_version="1", crs="EPSG:32650",
            resolution="30 m", license="test", attribution="test",
        )
    ]
    ranked = score_and_rank(_geojson(), request, provenance)
    assert ranked[0].rank == 1
    assert 0 <= ranked[0].overall_score <= 100
    assert ranked[0].score_breakdown["capacity_area_check"]["status"] == "unavailable"
    assert "capacity_area_check_unavailable" in ranked[0].key_risks
    aoi = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[115.9, 38.9], [116.1, 38.9], [116.1, 39.1], [115.9, 39.1], [115.9, 38.9]]]}}],
    }
    assert GISExecutor().validate_candidate_geometry(_geojson(), aoi).status == "success"

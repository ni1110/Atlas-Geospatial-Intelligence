from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from geoagent.orchestration.harness import AgentExecutionHarness, ConflictResolver, TaskLedger
from geoagent.orchestration.models import AgentResult, ArtifactRef, EvidenceRef, GeoGoalUnderstanding, GeoIntentDecision, GoalContract, LLMPlanPatchDraft, LLMTaskGraphDraft, LLMTaskNodeDraft, PlanPatch, ReviewResult, TaskGraph, TaskNode
from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.planning import DynamicGeoPlanner, NoProgressGuard
from geoagent.orchestration.routing import HierarchicalIntentRouter, deterministic_route, parse_goal_contract
from geoagent.orchestration.security import ArtifactCommitGate, PolicyDenied, WorkspaceManager, default_capabilities, sha256_file
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.api import app
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM
from renewable_site_agent.config import PATHS


def contract(query: str) -> GoalContract:
    decision = deterministic_route(query)
    assert not decision.needs_clarification
    return parse_goal_contract(query, decision)


class OfflineScriptedDecisionLLM:
    """Contract test double; never reported as a live-model result."""

    provider = "offline-scripted-zhipu"
    model_id = "glm-contract-test"

    def __init__(self, *, fail: bool = False) -> None:
        self.call_count = 0
        self.fail = fail

    def generate(self, prompt: str, output_model):
        self.call_count += 1
        if self.fail:
            raise TimeoutError("injected LLM timeout")
        if output_model is GeoGoalUnderstanding:
            return GeoGoalUnderstanding(
                decision=GeoIntentDecision(
                    intent="terrain_analysis",
                    subtype="unknown",
                    confidence=0.97,
                    required_agent_roles=["data", "terrain"],
                    missing_fields=[],
                    needs_clarification=False,
                    route_mode="llm",
                    reason="offline contract response",
                ),
                primary_goal="Analyze terrain slope in Gansu",
                region_or_aoi={"regions": ["Gansu"]},
                explicit_user_requirements=["Analyze terrain slope in Gansu"],
                expected_outputs=["terrain_statistics"],
            )
        if output_model is LLMTaskGraphDraft:
            return LLMTaskGraphDraft(
                rationale="Request-specific two-stage terrain graph",
                nodes=[
                    LLMTaskNodeDraft(
                        task_id="glm-aoi-data",
                        agent_role="data",
                        objective="Acquire the minimum Gansu DEM AOI window",
                        allowed_tools=["acquire_aoi_window"],
                        expected_outputs=["data_artifact"],
                    ),
                    LLMTaskNodeDraft(
                        task_id="glm-slope",
                        agent_role="terrain",
                        objective="Calculate slope from the acquired DEM window",
                        dependencies=["glm-aoi-data"],
                        required_inputs=["data_artifact"],
                        allowed_tools=["calculate_slope"],
                        expected_outputs=["slope"],
                    ),
                ],
            )
        if output_model is LLMPlanPatchDraft:
            return LLMPlanPatchDraft(
                replace_task_ids=["glm-slope"],
                replacement_nodes=[LLMTaskNodeDraft(
                    task_id="glm-slope", agent_role="terrain",
                    objective="Retry the failed slope calculation using the accepted cached DEM",
                    dependencies=["glm-aoi-data"], required_inputs=["data_artifact"],
                    allowed_tools=["calculate_slope"], expected_outputs=["slope"],
                )],
                reason="Local repair of the failed terrain node only",
            )
        raise AssertionError(output_model)


def test_llm_first_goal_and_planner_are_wired_into_orchestrator(tmp_path: Path) -> None:
    scripted = OfflineScriptedDecisionLLM()
    result = GeoOrchestratorAgent(
        router=HierarchicalIntentRouter(llm=scripted),
        planner=DynamicGeoPlanner(llm=scripted),
        state_store=MultiAgentStateStore(tmp_path / "llm-first.sqlite3"),
    ).plan("Analyze terrain slope in Gansu", use_llm=True)

    assert result.intent.route_mode == "llm"
    assert result.metrics["route_mode"] == "llm"
    assert result.metrics["plan_mode"] == "llm"
    assert result.task_graph.planner_mode == "llm"
    assert [node.task_id for node in result.task_graph.nodes] == ["glm-aoi-data", "glm-slope"]
    assert result.task_graph.planning_metadata["llm_available"] is True
    assert result.task_graph.planning_metadata["provider"] == "offline-scripted-zhipu"
    assert scripted.call_count == 2


def test_formal_geo_job_api_exposes_planned_and_actual_execution() -> None:
    response = TestClient(app).post(
        "/v1/geo/jobs",
        json={"user_query": "Analyze Gansu terrain slope", "use_live_llm": False, "resume": False},
    )
    assert response.status_code == 201
    created = response.json()
    snapshot = TestClient(app).get(created["status_url"]).json()
    assert snapshot["status"] == "SUCCEEDED"
    assert snapshot["route_mode"] == "deterministic"
    assert snapshot["plan_mode"] == "deterministic"
    assert snapshot["selected_agents"] == ["data", "terrain"]
    assert {item["execution_class"] for item in snapshot["actual_tool_calls"]} == {"CACHE_REUSED"}
    assert len(snapshot["actual_attempts"]) == 2
    terrain_outputs = [output for preview in snapshot["artifact_previews"] for output in preview["outputs"]]
    assert any(output.get("elevation_mean_m") is not None for output in terrain_outputs)
    assert snapshot["metrics"]["goal_coverage_status"] == "PASS"


def test_formal_geo_job_executes_buffer_and_intersection(tmp_path: Path) -> None:
    fixture_root = PATHS.temp / "formal-api-fixtures"
    fixture_root.mkdir(parents=True, exist_ok=True)
    source = fixture_root / "source.geojson"
    mask = fixture_root / "mask.geojson"
    source.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [103.05, 37.65]}}), encoding="utf-8")
    mask.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[103.04,37.64],[103.06,37.64],[103.06,37.66],[103.04,37.66],[103.04,37.64]]]}}), encoding="utf-8")
    response = TestClient(app).post(
        "/v1/geo/jobs",
        json={
            "user_query": "Buffer the input point by 500m then intersect it with the mask",
            "use_live_llm": False, "resume": False,
            "constraints": {"buffer_distance_m": 500},
            "input_artifacts": [{"path": str(source)}, {"path": str(mask)}],
        },
    )
    assert response.status_code == 201
    snapshot = TestClient(app).get(response.json()["status_url"]).json()
    assert [call["tool"] for call in snapshot["actual_tool_calls"]] == ["buffer", "intersect"]
    assert all(call["execution_class"] == "OFFLINE_LOCAL_DATA_EXECUTED" for call in snapshot["actual_tool_calls"])
    geojson_outputs = [output for preview in snapshot["artifact_previews"] for output in preview["outputs"] if output.get("geojson")]
    assert geojson_outputs[-1]["geojson"]["type"] == "FeatureCollection"


def test_llm_local_replan_does_not_rerun_successful_nodes(tmp_path: Path) -> None:
    scripted = OfflineScriptedDecisionLLM()
    calls = {"data": 0, "terrain": 0}

    def flaky(task: TaskNode, _contract: GoalContract, _inputs: list[ArtifactRef]) -> dict[str, Any]:
        calls[task.agent_role] += 1
        if task.agent_role == "terrain" and calls["terrain"] <= 2:
            raise RuntimeError("injected recoverable terrain failure")
        return {"task": task.task_id, "status": "ok"}

    result = GeoOrchestratorAgent(
        router=HierarchicalIntentRouter(llm=scripted),
        planner=DynamicGeoPlanner(llm=scripted),
        state_store=MultiAgentStateStore(tmp_path / "llm-local-replan.sqlite3"),
    ).execute(
        "Analyze terrain slope in Gansu", use_llm=True, resume=False,
        handlers={"data": flaky, "terrain": flaky},
    )

    assert result.status == "SUCCEEDED"
    assert len(result.replans) >= 1
    assert all(patch.planner_mode == "llm" for patch in result.replans)
    assert all(patch.replace_task_ids == ["glm-slope"] for patch in result.replans)
    assert calls == {"data": 1, "terrain": 3}


def test_llm_planner_prompt_contains_governed_required_roles() -> None:
    class CapturingLLM(OfflineScriptedDecisionLLM):
        prompt = ""

        def generate(self, prompt: str, output_model):
            self.prompt = prompt
            return super().generate(prompt, output_model)

    llm = CapturingLLM()
    graph = DynamicGeoPlanner(llm=llm).create(contract("分析甘肃坡度"), use_llm=True)

    assert graph.planner_mode == "llm"
    assert '"required_agent_roles": ["data", "terrain"]' in llm.prompt


def test_goal_understanding_accepts_flat_provider_decision() -> None:
    value = GeoGoalUnderstanding.model_validate(
        {
            "intent": "terrain_analysis",
            "subtype": "unknown",
            "confidence": 0.9,
            "required_agent_roles": ["data", "terrain"],
            "missing_fields": [],
            "needs_clarification": False,
            "route_mode": "llm",
            "reason": "provider flattened nested decision",
            "primary_goal": "Analyze terrain",
        }
    )

    assert value.decision.intent == "terrain_analysis"
    assert value.primary_goal == "Analyze terrain"


def test_goal_understanding_accepts_empty_nested_decision_and_string_lists() -> None:
    value = GeoGoalUnderstanding.model_validate({
        "decision": {},
        "intent": "general_gis", "subtype": "unknown", "confidence": 0.9,
        "required_agent_roles": '["gis_analysis"]', "missing_fields": "",
        "needs_clarification": False, "route_mode": "llm", "reason": "flat provider payload",
        "primary_goal": "Buffer a point", "expected_outputs": "vector",
    })
    assert value.decision.required_agent_roles == ["gis_analysis"]
    assert value.decision.missing_fields == []
    assert value.expected_outputs == ["vector"]


def test_llm_planner_normalizes_reviewer_inputs_to_dependency_ids() -> None:
    scripted = OfflineScriptedDecisionLLM()
    draft = LLMTaskGraphDraft(
        nodes=[
            LLMTaskNodeDraft(
                task_id="analysis",
                agent_role="terrain",
                objective="Calculate terrain evidence",
                allowed_tools=["calculate_slope"],
                expected_outputs=["坡度结果"],
            ),
            LLMTaskNodeDraft(
                task_id="review",
                agent_role="reviewer",
                objective="Review terrain evidence",
                dependencies=["analysis"],
                required_inputs=["坡度结果"],
                allowed_tools=["validate_provenance"],
            ),
            LLMTaskNodeDraft(
                task_id="data",
                agent_role="data",
                objective="Acquire terrain data",
                allowed_tools=["acquire_aoi_window"],
            ),
        ]
    )
    goal = contract("分析甘肃坡度")
    graph = DynamicGeoPlanner()._govern_llm_draft(goal, draft, None, scripted)  # noqa: SLF001

    assert graph.node("review").required_inputs == ["analysis"]


def test_llm_task_graph_ignores_non_node_provider_commentary() -> None:
    draft = LLMTaskGraphDraft.model_validate(
        {
            "nodes": [
                {
                    "task_id": "data",
                    "agent_role": "data",
                    "objective": "Acquire AOI data",
                    "allowed_tools": ["acquire_aoi_window"],
                },
                "The following tasks can run in parallel.",
                json.dumps(
                    {
                        "task_id": "terrain",
                        "agent_role": "terrain",
                        "objective": "Analyze terrain",
                        "dependencies": ["data"],
                        "allowed_tools": ["calculate_slope"],
                    }
                ),
            ]
        }
    )

    assert [node.task_id for node in draft.nodes] == ["data", "terrain"]


def test_llm_planner_rejects_role_tool_budget_overflow() -> None:
    nodes = [
        LLMTaskNodeDraft(
            task_id=f"gis-{index}",
            agent_role="gis_analysis",
            objective=f"GIS stage {index}",
            allowed_tools=list((
                "buffer", "intersect", "clip", "dissolve", "spatial_join",
                "zonal_statistics", "nearest_distance", "spatial_exclusion",
            )),
        )
        for index in range(2)
    ]
    goal = contract("给道路做500m buffer")

    with pytest.raises(ValueError, match="exceeds specialist tool budgets"):
        DynamicGeoPlanner()._govern_llm_draft(  # noqa: SLF001
            goal,
            LLMTaskGraphDraft(nodes=nodes),
            None,
            OfflineScriptedDecisionLLM(),
        )


def test_llm_timeout_falls_back_to_deterministic_decision_and_plan(tmp_path: Path) -> None:
    failing = OfflineScriptedDecisionLLM(fail=True)
    result = GeoOrchestratorAgent(
        router=HierarchicalIntentRouter(llm=failing),
        planner=DynamicGeoPlanner(llm=failing),
        state_store=MultiAgentStateStore(tmp_path / "llm-fallback.sqlite3"),
    ).plan("Analyze terrain slope in Gansu", use_llm=True)

    assert result.intent.route_mode == "deterministic"
    assert result.metrics["plan_mode"] == "deterministic"
    assert result.task_graph.planning_metadata["mode"] == "deterministic_fallback"
    assert result.task_graph.planning_metadata["llm_available"] is True
    assert "injected LLM timeout" in result.task_graph.planning_metadata["llm_error"]
    assert failing.call_count == 4
    assert result.task_graph is not None


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("给道路做500m buffer", "general_gis"),
        ("找甘肃 DEM 数据", "data_discovery"),
        ("分析甘肃坡度", "terrain_analysis"),
        ("搜索甘肃 Sentinel 影像并计算 NDVI", "remote_sensing"),
        ("帮我做甘肃光伏选址", "renewable_siting"),
        ("把结果添加到地图", "map_visualization"),
        ("比较甘肃和青海的地形、太阳能资源和基础设施，哪个更适合光伏", "multi_domain"),
    ],
)
def test_hierarchical_router_covers_geoagent_intents(query: str, intent: str) -> None:
    assert deterministic_route(query).intent == intent


@pytest.mark.parametrize(
    ("query", "required_roles"),
    [
        ("分析该区域坡度和 Sentinel 影像可用性", {"data", "terrain", "remote_sensing", "reviewer"}),
        ("分析道路可达性和坡度", {"data", "gis_analysis", "terrain", "reviewer"}),
        ("分析地形、道路和遥感条件", {"data", "terrain", "gis_analysis", "remote_sensing", "reviewer"}),
    ],
)
def test_deterministic_multidomain_router_unions_explicit_roles(query: str, required_roles: set[str]) -> None:
    decision = deterministic_route(query)
    assert decision.intent == "multi_domain"
    assert set(decision.required_agent_roles) == required_roles


def test_deterministic_single_remote_request_does_not_expand_to_terrain() -> None:
    decision = deterministic_route("搜索甘肃 Sentinel-2 影像")
    assert decision.intent == "remote_sensing"
    assert set(decision.required_agent_roles) == {"data", "remote_sensing"}


def test_deterministic_multidomain_plan_contains_terrain_and_remote() -> None:
    query = "对甘肃小范围 AOI 的地形与遥感可用性进行综合分析，并生成带来源记录的摘要。"
    result = GeoOrchestratorAgent().plan(query, use_llm=False)
    assert result.intent.intent == "multi_domain"
    assert {node.agent_role for node in result.task_graph.nodes} >= {"data", "terrain", "remote_sensing", "reviewer", "map_report"}


def test_goal_coverage_rejects_missing_required_domain_evidence() -> None:
    query = "对甘肃小范围 AOI 的地形与遥感可用性进行综合分析"
    planned = GeoOrchestratorAgent().plan(query, use_llm=False)
    results = [
        AgentResult(task_id="data", agent_id="data-agent", status="SUCCEEDED", summary="ok", evidence_refs=[EvidenceRef(source="data-agent", summary="data")]),
        AgentResult(task_id="remote", agent_id="remote_sensing-agent", status="SUCCEEDED", summary="ok", evidence_refs=[EvidenceRef(source="remote_sensing-agent", summary="remote")]),
        AgentResult(task_id="review", agent_id="reviewer-agent", status="SUCCEEDED", summary="ok", artifact_refs=[]),
    ]
    review, required, covered = GeoOrchestratorAgent._validate_goal_coverage(planned, results)  # noqa: SLF001
    assert review.status == "FAIL"
    assert "terrain_evidence" in review.missing_evidence
    assert "terrain" in required
    assert "terrain" not in covered


def test_missing_zhipu_key_records_explainable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ZHIPUAI_API_KEY", "ZAI_API_KEY", "GEOAGENT_MODEL_API_KEY", "OPENAI_API_KEY",
        "OPENAI_CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "OPENROUTER_API_KEY", "VLLM_BASE_URL", "OLLAMA_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEOAGENT_MODEL_PROVIDER", "zhipu")
    monkeypatch.setenv("GEOAGENT_MODEL_ID", "glm-4.5-flash")
    assert StrandsStructuredLLM.from_environment() is None
    assert StrandsStructuredLLM.last_environment_error == (
        "ZHIPU_CREDENTIAL_MISSING: provider=zhipu, model=glm-4.5-flash, key_present=false"
    )
    result = GeoOrchestratorAgent().plan("分析甘肃坡度", use_llm=True)
    assert result.metrics["goal_intent_llm_error"].startswith("ZHIPU_CREDENTIAL_MISSING")
    assert result.task_graph.planning_metadata["llm_error"].startswith("ZHIPU_CREDENTIAL_MISSING")


def test_goal_contract_is_immutable_and_hashed() -> None:
    value = contract("分析甘肃坡度")
    assert value.contract_hash
    with pytest.raises(ValidationError):
        value.primary_goal = "changed"  # type: ignore[misc]


def test_clarification_mints_new_contract_hash(tmp_path: Path) -> None:
    prior = contract("分析甘肃坡度")
    updated = GeoOrchestratorAgent(state_store=MultiAgentStateStore(tmp_path / "clarify.sqlite3")).clarify(prior, "AOI 是甘肃兰州")
    assert updated.goal_id == prior.goal_id
    assert updated.version == prior.version + 1
    assert updated.contract_hash and updated.contract_hash != prior.contract_hash


def test_dynamic_plans_differ_and_simple_gis_has_no_irrelevant_agents() -> None:
    planner = DynamicGeoPlanner()
    simple = planner.create(contract("给道路做500m buffer"))
    terrain = planner.create(contract("分析甘肃坡度"))
    renewable = planner.create(contract("帮我做甘肃光伏选址"))
    assert len({simple.plan_hash, terrain.plan_hash, renewable.plan_hash}) == 3
    assert {node.agent_role for node in simple.nodes} == {"gis_analysis"}
    assert "renewable_siting" not in {node.agent_role for node in terrain.nodes}
    assert {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer"}.issubset({node.agent_role for node in renewable.nodes})


def test_distance_request_selects_nearest_distance_tool() -> None:
    graph = DynamicGeoPlanner().create(contract("计算甘肃道路和变电站距离"))
    assert [node.allowed_tools for node in graph.nodes] == [["nearest_distance"]]


def test_dynamic_plan_builds_official_strands_graph(tmp_path: Path) -> None:
    class StrandsExecutor:
        async def invoke_async(self, prompt=None, **kwargs):  # pragma: no cover - adapter build only
            raise NotImplementedError

        def __call__(self, prompt=None, **kwargs):  # pragma: no cover - adapter build only
            raise NotImplementedError

        async def stream_async(self, prompt=None, **kwargs):  # pragma: no cover - adapter build only
            if False:
                yield None

    goal = contract("分析甘肃坡度")
    graph = DynamicGeoPlanner().create(goal)
    official = AgentExecutionHarness(store=MultiAgentStateStore(tmp_path / "strands.sqlite3")).to_strands_graph(
        graph,
        {node.task_id: StrandsExecutor() for node in graph.nodes},
    )
    assert set(official.nodes) == {node.task_id for node in graph.nodes}
    assert {(edge.from_node.node_id, edge.to_node.node_id) for edge in official.edges} == {("terrain-data", "terrain-analysis")}


def test_task_graph_rejects_cycle() -> None:
    goal = contract("分析甘肃坡度")
    with pytest.raises(ValidationError):
        TaskGraph(
            goal_id=goal.goal_id,
            nodes=[
                TaskNode(task_id="a", goal_id=goal.goal_id, agent_role="terrain", objective="a", dependencies=["b"]),
                TaskNode(task_id="b", goal_id=goal.goal_id, agent_role="data", objective="b", dependencies=["a"]),
            ],
        )


def make_workspace(tmp_path: Path):
    manager = WorkspaceManager(runtime_root=tmp_path / "jobs")
    workspace = manager.create("job-security", ["data-agent", "gis_analysis-agent", "reviewer-agent", "orchestrator-agent"])
    return manager, workspace, default_capabilities(workspace)


def test_specialist_delegation_is_denied(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_delegate(workspace=workspace, caller_id="data-agent", caller_policy=policies["data"], target_role="gis_analysis")
    assert caught.value.decision.error_code == "DELEGATION_DENIED"


def test_cross_agent_write_is_denied(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    target = workspace.scratch("gis_analysis-agent") / "attack.txt"
    with pytest.raises(PolicyDenied) as caught:
        manager.write_bytes(workspace=workspace, policy=policies["data"], agent_id="data-agent", path=target, data=b"attack")
    assert caught.value.decision.error_code == "CROSS_AGENT_WRITE"
    assert not target.exists()


@pytest.mark.parametrize("protected", ["config", "models", ".git"])
def test_protected_project_paths_are_denied(tmp_path: Path, protected: str) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate=PATHS.project_root / protected / "attack.txt")
    assert caught.value.decision.error_code == "PROTECTED_PATH"


def test_reviewer_direct_final_write_is_denied(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    with pytest.raises(PolicyDenied) as caught:
        manager.write_bytes(workspace=workspace, policy=policies["reviewer"], agent_id="reviewer-agent", path=workspace.final / "result.json", data=b"attack")
    assert caught.value.decision.error_code == "DIRECT_FINAL_WRITE"


def test_parent_traversal_is_denied(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate="../../escape.txt")
    assert caught.value.decision.error_code == "PATH_ESCAPE"


def test_symlink_escape_is_denied_where_supported(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    link = workspace.scratch("data-agent") / "outside-link"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows policy")
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate=link / "attack.txt")
    assert caught.value.decision.error_code == "PATH_ESCAPE"


def test_unauthorized_tool_and_provider_are_denied(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    with pytest.raises(PolicyDenied) as tool_denial:
        manager.authorize_tool(workspace=workspace, policy=policies["data"], agent_id="data-agent", tool_name="score_candidates")
    assert tool_denial.value.decision.error_code == "UNAUTHORIZED_TOOL"
    with pytest.raises(PolicyDenied) as provider_denial:
        manager.authorize_tool(workspace=workspace, policy=policies["terrain"], agent_id="terrain-agent", tool_name="acquire_dem", provider="nasa_power")
    assert provider_denial.value.decision.error_code == "UNAUTHORIZED_PROVIDER"


def test_legacy_side_effect_wrapper_blocks_protected_path(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    policy = policies["data"].model_copy(update={"allowed_tools": (*policies["data"].allowed_tools, "save_legacy_layer")})
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_legacy_tool_call(
            workspace=workspace, policy=policy, agent_id="data-agent",
            tool_name="save_legacy_layer",
            arguments={"output_path": str(PATHS.project_root / "config" / "attack.geojson")},
            confirmed=True,
        )
    assert caught.value.decision.error_code == "PROTECTED_PATH"


def test_legacy_side_effect_wrapper_requires_confirmation(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    policy = policies["data"].model_copy(update={"allowed_tools": (*policies["data"].allowed_tools, "delete_legacy_layer")})
    with pytest.raises(PolicyDenied) as caught:
        manager.authorize_legacy_tool_call(
            workspace=workspace, policy=policy, agent_id="data-agent",
            tool_name="delete_legacy_layer", arguments={}, requires_confirmation=True,
        )
    assert caught.value.decision.error_code == "REQUIRES_CONFIRMATION"


def staged_artifact(manager: WorkspaceManager, workspace, policy, content: bytes = b"ok", filename: str = "result.json") -> ArtifactRef:
    path = manager.write_bytes(workspace=workspace, policy=policy, agent_id="data-agent", path=workspace.agent_staging("data-agent") / filename, data=content)
    return ArtifactRef(job_id=workspace.job_id, task_id="task-1", producer_agent="data-agent", artifact_type="data_artifact", path=str(path), checksum_sha256=sha256_file(path))


def test_artifact_commit_gate_promotes_atomically(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    artifact = staged_artifact(manager, workspace, policies["data"])
    committed = ArtifactCommitGate(manager).commit(workspace=workspace, contract=GoalContract(primary_goal="x", intent="general_gis"), artifact=artifact, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set())
    assert Path(committed.path).is_file()
    assert workspace.final in Path(committed.path).parents
    assert not Path(artifact.path).exists()


def test_commit_rejects_checksum_and_version_conflict(tmp_path: Path) -> None:
    manager, workspace, policies = make_workspace(tmp_path)
    gate = ArtifactCommitGate(manager)
    broken = staged_artifact(manager, workspace, policies["data"]).model_copy(update={"checksum_sha256": "0" * 64})
    with pytest.raises(PolicyDenied) as checksum_denial:
        gate.commit(workspace=workspace, contract=GoalContract(primary_goal="x", intent="general_gis"), artifact=broken, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set())
    assert checksum_denial.value.decision.error_code == "COMMIT_REJECTED"
    first = staged_artifact(manager, workspace, policies["data"], b"one", "same.json")
    gate.commit(workspace=workspace, contract=GoalContract(primary_goal="x", intent="general_gis"), artifact=first, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set())
    second = staged_artifact(manager, workspace, policies["data"], b"two", "same.json")
    with pytest.raises(PolicyDenied) as version_denial:
        gate.commit(workspace=workspace, contract=GoalContract(primary_goal="x", intent="general_gis"), artifact=second, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set())
    assert version_denial.value.decision.error_code == "COMMIT_VERSION_CONFLICT"


def test_task_ledger_reuses_valid_artifact(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text("{}", encoding="utf-8")
    artifact = ArtifactRef(job_id="j", task_id="t", producer_agent="a", artifact_type="x", path=str(path), checksum_sha256=sha256_file(path))
    result = AgentResult(task_id="t", agent_id="a", status="SUCCEEDED", summary="ok", artifact_refs=[artifact])
    ledger = TaskLedger()
    ledger.put("signature", result)
    assert ledger.lookup("signature").status == "REUSED"


def test_harness_crash_resume_reuses_successful_nodes(tmp_path: Path) -> None:
    store = MultiAgentStateStore(tmp_path / "resume.sqlite3")
    goal = contract("分析甘肃坡度")
    graph = DynamicGeoPlanner().create(goal)
    first_calls = {"data": 0, "terrain": 0}

    def handler(task, goal_contract, inputs):
        first_calls[task.agent_role] += 1
        return {"task": task.task_id, "inputs": [item.artifact_id for item in inputs]}

    with pytest.raises(RuntimeError, match="INJECTED_WORKER_CRASH"):
        AgentExecutionHarness(store=store).execute_graph(
            job_id="resume-job",
            contract=goal,
            graph=graph,
            handlers={"data": handler, "terrain": handler},
            crash_after_completed=1,
        )
    assert first_calls == {"data": 1, "terrain": 0}

    results, review = AgentExecutionHarness(store=store).execute_graph(
        job_id="resume-job",
        contract=goal,
        graph=graph,
        handlers={"data": handler, "terrain": handler},
        resume=True,
    )
    assert review.status == "PASS"
    assert results[0].status == "REUSED"
    assert first_calls == {"data": 1, "terrain": 1}


def test_orchestrator_replans_only_failed_node(tmp_path: Path) -> None:
    calls = {"data": 0, "terrain": 0}

    def flaky(task, goal_contract, inputs):
        calls[task.agent_role] += 1
        if task.agent_role == "terrain" and calls["terrain"] == 1:
            raise RuntimeError("injected terrain failure")
        return {"task": task.task_id, "inputs": [item.artifact_id for item in inputs]}

    result = GeoOrchestratorAgent(state_store=MultiAgentStateStore(tmp_path / "replan.sqlite3")).execute(
        "analyze Gansu terrain slope",
        use_llm=False,
        handlers={"data": flaky, "terrain": flaky},
    )
    assert result.status == "SUCCEEDED"
    assert len(result.replans) == 1
    assert calls == {"data": 1, "terrain": 2}
    assert any(item.status == "REUSED" and item.task_id == "terrain-data" for item in result.agent_results)


def test_no_progress_loop_detected() -> None:
    guard = NoProgressGuard(threshold=2)
    guard.record("same", [], {})
    with pytest.raises(RuntimeError, match="NO_PROGRESS_LOOP"):
        guard.record("same", [], {})


def test_conflict_resolver_preserves_goal_and_routes_review() -> None:
    resolver = ConflictResolver()
    goal = GoalContract(primary_goal="buffer 500m", intent="general_gis", hard_constraints={"buffer_distance_m": 500})
    conflict = resolver.resolve(contract=goal, proposals=[{"agent_id": "a", "buffer_distance_m": 1000}])
    assert conflict.resolution == "REVIEW"
    assert goal.hard_constraints["buffer_distance_m"] == 500


def test_orchestrator_executes_and_persists_dynamic_graph(tmp_path: Path) -> None:
    store = MultiAgentStateStore(tmp_path / "multi-agent.sqlite3")
    result = GeoOrchestratorAgent(state_store=store).execute("给道路做500m buffer并添加到地图", use_llm=False)
    assert result.status == "SUCCEEDED"
    assert {node.agent_role for node in result.task_graph.nodes} == {"gis_analysis", "map_report"}
    assert result.final_artifacts and Path(result.final_artifacts[0].path).is_file()
    counts = store.table_counts()
    assert counts["task_graphs"] == 1 and counts["agent_attempts"] == 2
    # Two staged artifacts plus the separately persisted CommitGate promotion.
    assert counts["artifacts"] == 3 and counts["workspace_events"] > 0


def test_independent_multi_domain_nodes_execute_in_parallel(tmp_path: Path) -> None:
    store = MultiAgentStateStore(tmp_path / "parallel.sqlite3")
    orchestrator = GeoOrchestratorAgent(state_store=store)
    intervals: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()
    data_barrier = threading.Barrier(2)
    analysis_barrier = threading.Barrier(4)
    def slow(task, goal, inputs):
        task_started = time.perf_counter()
        if task.agent_role == "data":
            data_barrier.wait(timeout=3)
        elif task.agent_role in {"terrain", "gis_analysis"}:
            analysis_barrier.wait(timeout=3)
        time.sleep(0.05)
        task_finished = time.perf_counter()
        with lock:
            intervals[task.task_id] = (task_started, task_finished)
        return {"task": task.task_id}
    result = orchestrator.execute("比较甘肃和青海的地形、太阳能资源和基础设施，哪个更适合光伏", use_llm=False, handlers={role: slow for role in ("data", "terrain", "gis_analysis", "renewable_siting", "map_report")})
    assert result.status == "SUCCEEDED"
    assert len(result.task_graph.nodes) == 9
    first, second = intervals["compare-1-data"], intervals["compare-2-data"]
    assert max(first[0], second[0]) < min(first[1], second[1])
    terrain, infrastructure = intervals["compare-1-terrain"], intervals["compare-1-infrastructure"]
    assert max(terrain[0], infrastructure[0]) < min(terrain[1], infrastructure[1])


def test_geoagent_api_exposes_dynamic_plan(tmp_path: Path) -> None:
    client = TestClient(app)
    response = client.post("/geoagent/orchestrate", json={"query": "分析甘肃坡度", "execute": False, "use_live_llm": False})
    assert response.status_code == 200
    value = response.json()
    assert value["intent"]["intent"] == "terrain_analysis"
    assert [node["agent_role"] for node in value["task_graph"]["nodes"]] == ["data", "terrain"]
    demo = client.get("/geoagent/demo")
    assert demo.status_code == 200
    assert 'id="map"' in demo.text
    assert 'id="query"' in demo.text
    assert 'id="agent-details"' in demo.text
    assert "查看技术执行详情" in demo.text
    assert "Advanced Debug · 原始 JSON" in demo.text
    assert "L.geoJSON" in demo.text
    assert "fetch('/v1/geo/jobs'" in demo.text
    assert "/v1/geo/jobs/${encodeURIComponent(jobId)}" in demo.text
    assert "/clarification`" in demo.text
    assert 'id="clarification-panel"' in demo.text
    assert 'id="map-legend" class="map-legend" hidden' in demo.text
    assert "function updateLegend" in demo.text
    assert "等待你补充信息，分析尚未开始" in demo.text
    assert "分析未完整完成" not in demo.text
    assert "Renewable Demo" in demo.text


def test_geo_clarification_continues_same_job_and_goal() -> None:
    client = TestClient(app)
    created = client.post(
        "/v1/geo/jobs",
        json={
            "user_query": "Please help with a geospatial task",
            "region_or_aoi": {
                "bbox": [102.99885031030179, 37.59704374600004, 103.09885031030178, 37.697043746000034],
                "crs": "EPSG:4326",
            },
            "use_live_llm": False,
            "resume": False,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    waiting = client.get(f"/v1/geo/jobs/{job_id}").json()
    assert waiting["status"] == "NEEDS_CLARIFICATION"
    assert waiting["user_status"] == "WAITING_FOR_USER"
    assert waiting["clarification_required"] is True
    assert waiting["clarification_reason"]
    assert waiting["goal_contract"]["region_or_aoi"]["bbox"][0] == 102.99885031030179
    assert waiting["clarification"]["required"] is True
    assert waiting["clarification"]["count"] == 1
    assert waiting["clarification"]["items"][0]["question"]
    original_goal_id = waiting["goal_contract"]["goal_id"]
    missing_field = waiting["clarification"]["items"][0]["field"]

    continued = client.patch(
        f"/v1/geo/jobs/{job_id}/clarification",
        json={
            "answers": {missing_field: "Analyze terrain slope in the selected AOI"},
            "use_live_llm": False,
        },
    )
    assert continued.status_code == 202
    assert continued.json()["job_id"] == job_id
    completed = client.get(f"/v1/geo/jobs/{job_id}").json()
    assert completed["status"] == "SUCCEEDED"
    assert completed["goal_contract"]["goal_id"] == original_goal_id
    assert completed["goal_contract"]["version"] == 2
    assert completed["goal_contract"]["region_or_aoi"] == waiting["goal_contract"]["region_or_aoi"]
    assert completed["clarification"]["required"] is False
    assert completed["task_graph"]["nodes"]
    assert completed["actual_attempts"]

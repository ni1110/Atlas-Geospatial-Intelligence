"""Credential-free smoke tests used by the public Atlas CI workflow."""

from fastapi.testclient import TestClient

from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.routing import HierarchicalIntentRouter
from renewable_site_agent.api import app


def test_deterministic_router_is_available_without_provider_credentials(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    decision = HierarchicalIntentRouter().route(
        "Create a 500 meter buffer around the supplied point",
        use_llm=False,
    )

    assert decision.route_mode == "deterministic"
    assert decision.intent == "general_gis"
    assert decision.required_agent_roles == ["gis_analysis"]


def test_deterministic_planner_builds_request_specific_graphs(monkeypatch) -> None:
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    orchestrator = GeoOrchestratorAgent()

    buffer_plan = orchestrator.plan(
        "Create a 500 meter buffer around the supplied point",
        use_llm=False,
    )
    terrain_plan = orchestrator.plan(
        "Analyze terrain slope for bbox 100,30,101,31",
        use_llm=False,
    )

    assert buffer_plan.metrics["route_mode"] == "deterministic"
    assert buffer_plan.metrics["plan_mode"] == "deterministic"
    assert terrain_plan.metrics["route_mode"] == "deterministic"
    assert terrain_plan.metrics["plan_mode"] == "deterministic"
    assert buffer_plan.task_graph is not None
    assert terrain_plan.task_graph is not None
    assert [node.task_id for node in buffer_plan.task_graph.nodes] != [
        node.task_id for node in terrain_plan.task_graph.nodes
    ]
    assert {node.agent_role for node in terrain_plan.task_graph.nodes} == {
        "data",
        "terrain",
    }


def test_public_http_surfaces_are_available() -> None:
    client = TestClient(app)

    health = client.get("/health")
    demo = client.get("/geoagent/demo")
    docs = client.get("/docs")

    assert health.status_code == 200
    assert health.json()["canonical_pipeline"] is True
    assert demo.status_code == 200
    assert "Atlas" in demo.text
    assert 'id="map"' in demo.text
    assert 'id="query"' in demo.text
    assert docs.status_code == 200

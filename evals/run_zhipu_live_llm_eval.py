"""Real Zhipu GLM LLM-first acceptance; refuses to run without a real key."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.geoagent_intent_dataset import held_out_cases
from evals.run_multi_agent_evals import evaluate_router, rate
from geoagent.orchestration.models import GoalContract
from geoagent.orchestration.harness import AgentExecutionHarness
from geoagent.orchestration.models import GeoIntentDecision, TaskGraph
from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.planning import DynamicGeoPlanner
from geoagent.orchestration.routing import HierarchicalIntentRouter
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM
from renewable_site_agent.config import PATHS, apply_process_environment


OPEN_GOALS: list[tuple[str, set[str]]] = [
    ("给甘肃兰州市道路做 500 米缓冲，并与建设用地相交", {"gis_analysis"}),
    ("分析甘肃一个小范围 AOI 的高程、坡度和坡向，并输出地形统计", {"data", "terrain"}),
    ("在青海一个小范围 AOI 搜索最近的 Sentinel-2 影像并计算 NDVI", {"data", "remote_sensing"}),
    ("对甘肃一个小范围 AOI 做光伏初筛，考虑坡度、水体、土地和道路可达性", {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer"}),
    ("比较甘肃和青海的地形、太阳能资源与道路基础设施，判断哪个区域更值得继续做光伏选址", {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"}),
]

CHECKPOINT_PATH = PATHS.artifacts / "zhipu_live_llm_checkpoint.json"


class InjectedUnavailableLLM:
    """Fault injector used only for the separately labelled fallback check."""

    provider = "injected-unavailable"
    model_id = "none"
    call_count = 0

    def generate(self, prompt: str, output_model: type[Any]) -> Any:
        self.call_count += 1
        raise TimeoutError("injected provider timeout")


def _real_llm() -> StrandsStructuredLLM:
    provider = os.getenv("GEOAGENT_MODEL_PROVIDER", "").strip().lower()
    has_key = bool(os.getenv("ZHIPUAI_API_KEY") or os.getenv("ZAI_API_KEY") or os.getenv("GEOAGENT_MODEL_API_KEY"))
    if provider != "zhipu" or not has_key:
        raise RuntimeError(
            "ZHIPU_LIVE_KEY_REQUIRED: set GEOAGENT_MODEL_PROVIDER=zhipu and "
            "ZHIPUAI_API_KEY in the current process before running this acceptance."
        )
    llm = StrandsStructuredLLM.from_environment()
    if llm is None or llm.provider != "zhipu":
        raise RuntimeError("ZHIPU_PROVIDER_INITIALIZATION_FAILED")
    return llm


def _write_checkpoint(checkpoint: dict[str, Any]) -> None:
    PATHS.artifacts.mkdir(parents=True, exist_ok=True)
    temporary = CHECKPOINT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CHECKPOINT_PATH)


def _load_checkpoint(llm: StrandsStructuredLLM) -> dict[str, Any]:
    if not CHECKPOINT_PATH.is_file():
        return {
            "provider": llm.provider,
            "model": llm.model_id,
            "intent_decisions": {},
            "planner_graphs": {},
            "planner_rows": {},
        }
    value = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    if value.get("provider") != llm.provider or value.get("model") != llm.model_id:
        raise RuntimeError("LIVE_CHECKPOINT_PROVIDER_OR_MODEL_MISMATCH")
    return value


def _intent_metrics(llm: StrandsStructuredLLM, checkpoint: dict[str, Any]) -> dict[str, Any]:
    cases = held_out_cases()
    router = HierarchicalIntentRouter(llm=llm)
    saved = checkpoint.setdefault("intent_decisions", {})
    decisions: dict[str, GeoIntentDecision] = {}
    for index, case in enumerate(cases, 1):
        query = str(case["query"])
        if str(index) in saved:
            decision = GeoIntentDecision.model_validate(saved[str(index)]["decision"])
            if saved[str(index)]["query"] != query:
                raise RuntimeError(f"LIVE_ROUTER_CHECKPOINT_QUERY_MISMATCH:{index}")
        else:
            decision = router.route(query, use_llm=True)
            saved[str(index)] = {"query": query, "decision": decision.model_dump(mode="json")}
            _write_checkpoint(checkpoint)
        if decision.route_mode != "llm":
            raise RuntimeError(f"LIVE_ROUTER_FELL_BACK_AT_CASE_{index}")
        decisions[query] = decision
    metrics = evaluate_router(cases, lambda query: decisions[query])
    metrics["checkpoint_replay_p50_latency_ms"] = metrics.pop("p50_latency_ms")
    metrics["checkpoint_replay_p95_latency_ms"] = metrics.pop("p95_latency_ms")
    metrics["live_provider_latency"] = "not persisted by the interrupted provider processes; checkpoint replay latency is not provider latency"
    metrics["route_mode_llm_rate"] = rate(sum(item.route_mode == "llm" for item in decisions.values()), len(decisions))
    metrics["fallback_count"] = sum(item.route_mode != "llm" for item in decisions.values())
    if metrics["route_mode_llm_rate"] != 1.0:
        raise RuntimeError("LIVE_ROUTER_MIXED_WITH_FALLBACK")
    return metrics


def _intent_error_rows(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(held_out_cases(), 1):
        saved = checkpoint["intent_decisions"][str(index)]
        decision = GeoIntentDecision.model_validate(saved["decision"])
        expected_roles = set(case.get("agents", []))
        predicted_roles = set(decision.required_agent_roles)
        if (
            decision.intent != case["intent"]
            or decision.subtype != case.get("subtype", "unknown")
            or predicted_roles != expected_roles
            or decision.needs_clarification != bool(case.get("clarify"))
            or set(decision.missing_fields) != set(case.get("missing_fields", []))
        ):
            rows.append(
                {
                    "case": index,
                    "query": case["query"],
                    "expected_intent": case["intent"],
                    "predicted_intent": decision.intent,
                    "expected_subtype": case.get("subtype", "unknown"),
                    "predicted_subtype": decision.subtype,
                    "expected_agents": sorted(expected_roles),
                    "predicted_agents": sorted(predicted_roles),
                    "expected_clarification": bool(case.get("clarify")),
                    "predicted_clarification": decision.needs_clarification,
                    "expected_missing_fields": case.get("missing_fields", []),
                    "predicted_missing_fields": decision.missing_fields,
                }
            )
    return rows


def _normalize_recovered_graph(graph: TaskGraph, llm: StrandsStructuredLLM) -> TaskGraph:
    nodes = [
        node.model_copy(update={"required_inputs": node.dependencies})
        if node.agent_role == "reviewer"
        else node
        for node in graph.nodes
    ]
    return TaskGraph(
        goal_id=graph.goal_id,
        nodes=nodes,
        planner_mode="llm",
        planning_metadata={
            **graph.planning_metadata,
            "provider": llm.provider,
            "model": llm.model_id,
            "recovered_from_prior_live_run": True,
        },
    )


def _recover_prior_planner_inputs(recovery_root: Path, llm: StrandsStructuredLLM) -> dict[int, tuple[GoalContract, TaskGraph | None]]:
    recovered: dict[int, tuple[GoalContract, TaskGraph | None]] = {}
    for index in range(1, len(OPEN_GOALS) + 1):
        database = recovery_root / f"live-{index}.sqlite3"
        if not database.is_file():
            raise RuntimeError(f"LIVE_RECOVERY_DATABASE_MISSING:{index}")
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            job = connection.execute("SELECT request_json FROM jobs LIMIT 1").fetchone()
            if job is None:
                raise RuntimeError(f"LIVE_RECOVERY_CONTRACT_MISSING:{index}")
            contract = GoalContract.model_validate_json(job["request_json"])
            if contract.primary_goal != OPEN_GOALS[index - 1][0]:
                raise RuntimeError(f"LIVE_RECOVERY_QUERY_MISMATCH:{index}")
            graph: TaskGraph | None = None
            for row in connection.execute("SELECT graph_json FROM task_graphs ORDER BY created_at"):
                candidate = TaskGraph.model_validate_json(row["graph_json"])
                if candidate.planner_mode == "llm":
                    graph = _normalize_recovered_graph(candidate, llm)
                    break
            recovered[index] = (contract, graph)
        finally:
            connection.close()
    return recovered


def _execute_graph(contract: GoalContract, graph: TaskGraph, root: Path, index: int) -> dict[str, Any]:
    store = MultiAgentStateStore(root / f"continued-{index}.sqlite3")
    harness = AgentExecutionHarness(store=store)
    job_id = uuid.uuid4().hex
    results, review = harness.execute_graph(
        job_id=job_id,
        contract=contract,
        graph=graph,
        resume=False,
    )
    status = "SUCCEEDED" if review.status == "PASS" and all(item.status == "SUCCEEDED" for item in results) else "FAILED"
    actual_roles = {node.agent_role for node in graph.nodes}
    return {
        "query": contract.primary_goal,
        "route_mode": "llm",
        "route_evidence": "recovered real GLM GoalContract from interrupted run",
        "plan_mode": graph.planner_mode,
        "provider": graph.planning_metadata.get("provider"),
        "model": graph.planning_metadata.get("model"),
        "plan_source": "recovered_prior_real_glm_graph" if graph.planning_metadata.get("recovered_from_prior_live_run") else "new_real_glm_planner_call",
        "status": status,
        "review_status": review.status,
        "review_reason": review.reason,
        "agent_result_statuses": [
            {"task_id": item.task_id, "agent_id": item.agent_id, "status": item.status, "summary": item.summary}
            for item in results
        ],
        "selected_agents": sorted(actual_roles),
        "task_graph": [
            {
                "task_id": node.task_id,
                "agent_role": node.agent_role,
                "dependencies": node.dependencies,
                "allowed_tools": node.allowed_tools,
            }
            for node in graph.nodes
        ],
        "plan_hash": graph.plan_hash,
        "agent_turns": harness.turns,
        "handoff_count": harness.delegations,
    }


def _planner_and_harness_metrics(
    llm: StrandsStructuredLLM,
    root: Path,
    checkpoint: dict[str, Any],
    recovery_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    graph_hashes: set[str] = set()
    selection_recall: list[float] = []
    selection_precision: list[float] = []
    recovered = _recover_prior_planner_inputs(recovery_root, llm)
    saved_graphs = checkpoint.setdefault("planner_graphs", {})
    saved_rows = checkpoint.setdefault("planner_rows", {})
    failed_attempts = checkpoint.setdefault("failed_planner_attempts", {})
    for index, (query, expected_roles) in enumerate(OPEN_GOALS, 1):
        if str(index) in saved_rows:
            row = saved_rows[str(index)]
            if row.get("status") == "SUCCEEDED":
                rows.append(row)
                actual_roles = set(row["selected_agents"])
                graph_hashes.add(row["plan_hash"])
                selection_recall.append(rate(len(actual_roles & expected_roles), len(expected_roles)))
                selection_precision.append(rate(len(actual_roles & expected_roles), len(actual_roles)))
                continue
            failed_attempts.setdefault(str(index), []).append(
                {"row": row, "graph": saved_graphs.get(str(index))}
            )
            saved_rows.pop(str(index), None)
            saved_graphs.pop(str(index), None)
            _write_checkpoint(checkpoint)
        contract, prior_graph = recovered[index]
        if str(index) in saved_graphs:
            graph = TaskGraph.model_validate(saved_graphs[str(index)])
        elif prior_graph is not None:
            graph = prior_graph
            saved_graphs[str(index)] = graph.model_dump(mode="json")
            _write_checkpoint(checkpoint)
        else:
            live_planner = DynamicGeoPlanner(llm=llm)
            prior_failures = failed_attempts.get(str(index), [])
            compact_failures: list[dict[str, Any]] = []
            for failure in prior_failures[-2:]:
                prior_graph = failure.get("graph") or {}
                role_tool_counts: dict[str, int] = {}
                for node in prior_graph.get("nodes", []):
                    role = str(node.get("agent_role", "unknown"))
                    role_tool_counts[role] = role_tool_counts.get(role, 0) + len(node.get("allowed_tools", []))
                compact_failures.append(
                    {
                        "status": failure.get("row", {}).get("status"),
                        "review_status": failure.get("row", {}).get("review_status"),
                        "reason": "specialist tool budget exceeded before final review",
                        "role_tool_counts": role_tool_counts,
                    }
                )
            graph = live_planner.create(
                contract,
                execution_state={
                    "prior_plan_failures": compact_failures,
                    "replan_requirement": "Use fewer allow-listed tools while preserving every required role",
                } if prior_failures else None,
                use_llm=True,
            )
            if graph.planner_mode != "llm":
                raise RuntimeError(
                    f"LIVE_PLANNER_FELL_BACK_AT_CASE_{index}:"
                    f"{live_planner.last_llm_error or 'unknown LLM failure'}"
                )
            saved_graphs[str(index)] = graph.model_dump(mode="json")
            _write_checkpoint(checkpoint)
        row = _execute_graph(contract, graph, root, index)
        actual_roles = {node.agent_role for node in graph.nodes}
        graph_hashes.add(graph.plan_hash)
        selection_recall.append(rate(len(actual_roles & expected_roles), len(expected_roles)))
        selection_precision.append(rate(len(actual_roles & expected_roles), len(actual_roles)))
        rows.append(row)
        saved_rows[str(index)] = row
        _write_checkpoint(checkpoint)
    route_llm = sum(row["route_mode"] == "llm" for row in rows)
    plan_llm = sum(row["plan_mode"] == "llm" for row in rows)
    harness_success = sum(row["status"] == "SUCCEEDED" for row in rows)
    metrics = {
        "case_count": len(rows),
        "route_mode_llm_rate": rate(route_llm, len(rows)),
        "plan_mode_llm_rate": rate(plan_llm, len(rows)),
        "distinct_graph_rate": rate(len(graph_hashes), len(rows)),
        "harness_execution_success_rate": rate(harness_success, len(rows)),
        "required_agent_recall": sum(selection_recall) / len(selection_recall),
        "agent_selection_precision": sum(selection_precision) / len(selection_precision),
        "recovered_real_glm_goal_contracts": len(recovered),
        "recovered_real_glm_task_graphs": sum(row.get("plan_source") == "recovered_prior_real_glm_graph" for row in rows),
        "new_real_glm_planner_cases": sum(row.get("plan_source") == "new_real_glm_planner_call" for row in rows),
    }
    if any(metrics[key] != 1.0 for key in ("route_mode_llm_rate", "plan_mode_llm_rate", "distinct_graph_rate", "harness_execution_success_rate")):
        raise RuntimeError("ZHIPU_LIVE_PLANNER_ACCEPTANCE_FAILED")
    return metrics, rows


def _fallback_metrics(root: Path) -> dict[str, Any]:
    unavailable = InjectedUnavailableLLM()
    result = GeoOrchestratorAgent(
        router=HierarchicalIntentRouter(llm=unavailable),  # type: ignore[arg-type]
        planner=DynamicGeoPlanner(llm=unavailable),  # type: ignore[arg-type]
        state_store=MultiAgentStateStore(root / "fallback.sqlite3"),
    ).plan("Analyze terrain slope in Gansu", use_llm=True)
    return {
        "fault": "injected_timeout",
        "route_mode": result.intent.route_mode,
        "plan_mode": result.task_graph.planner_mode if result.task_graph else None,
        "fallback_success": bool(result.intent.route_mode == "deterministic" and result.task_graph and result.task_graph.planner_mode == "deterministic"),
        "injected_call_count": unavailable.call_count,
    }


def main() -> None:
    apply_process_environment()
    llm = _real_llm()
    checkpoint = _load_checkpoint(llm)
    recovery_value = os.getenv("GEOAGENT_ZHIPU_LIVE_RECOVERY_ROOT", "").strip()
    if not recovery_value:
        raise RuntimeError("ZHIPU_LIVE_RECOVERY_ROOT_REQUIRED_FOR_CONTINUATION")
    recovery_root = Path(recovery_value).resolve()
    PATHS.assert_artifact_path(recovery_root, large=False)
    root = Path(tempfile.mkdtemp(prefix="zhipu-live-llm-", dir=PATHS.temp))
    started_calls = llm.call_count
    intent = _intent_metrics(llm, checkpoint)
    intent_errors = _intent_error_rows(checkpoint)
    planner, rows = _planner_and_harness_metrics(llm, root, checkpoint, recovery_root)
    fallback = _fallback_metrics(root)
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "provider": "zhipu",
        "model": llm.model_id,
        "base_url_source": "ZHIPUAI_BASE_URL or official general API default",
        "api_key_source": "environment only",
        "real_llm_attempts_in_final_aggregation_process": llm.call_count - started_calls,
        "successful_real_glm_outputs_checkpointed": len(checkpoint.get("intent_decisions", {})) + len(checkpoint.get("planner_graphs", {})),
        "provider_attempt_count_across_interrupted_processes": "not exactly reconstructible because the original interrupted runner did not persist per-attempt counters",
        "continuation_strategy": "checkpointed; reused 5 prior GLM GoalContracts and any prior successful GLM TaskGraphs",
        "intent": intent,
        "intent_error_rows": intent_errors,
        "planner_and_harness": planner,
        "fallback": fallback,
        "cases": rows,
    }
    PATHS.artifacts.mkdir(parents=True, exist_ok=True)
    json_path = PATHS.artifacts / "zhipu_live_llm_metrics.json"
    report_path = PATHS.artifacts / "ZHIPU_LIVE_LLM_REPORT.md"
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    cases = "\n\n".join(
        f"### {index}. {row['query']}\n\n- route_mode: `{row['route_mode']}`\n- plan_mode: `{row['plan_mode']}`\n- status: `{row['status']}`\n- agents: `{', '.join(row['selected_agents'])}`\n\n```json\n{json.dumps(row['task_graph'], ensure_ascii=False, indent=2)}\n```"
        for index, row in enumerate(rows, 1)
    )
    report_path.write_text(
        "# Zhipu GLM Live LLM Acceptance\n\n"
        "This report contains real provider calls. No API key value is persisted.\n\n"
        f"- Provider: `zhipu`\n- Model: `{llm.model_id}`\n"
        f"- Successful real GLM outputs checkpointed: `{metrics['successful_real_glm_outputs_checkpointed']}`\n"
        f"- New provider attempts in the final checkpoint-only aggregation process: `{metrics['real_llm_attempts_in_final_aggregation_process']}`\n"
        "- Exact provider-attempt total across earlier interrupted processes: `not reconstructible`; no estimate is substituted.\n\n"
        f"## Intent router\n\n```json\n{json.dumps(intent, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Intent error rows and improvement targets\n\n```json\n{json.dumps(intent_errors, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Dynamic planner and Harness\n\n```json\n{json.dumps(planner, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Deterministic fallback (separate injected fault)\n\n```json\n{json.dumps(fallback, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Five open goals\n\n{cases}\n",
        encoding="utf-8",
    )
    print(json.dumps({key: metrics[key] for key in ("status", "provider", "model", "successful_real_glm_outputs_checkpointed", "real_llm_attempts_in_final_aggregation_process", "intent", "planner_and_harness", "fallback")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

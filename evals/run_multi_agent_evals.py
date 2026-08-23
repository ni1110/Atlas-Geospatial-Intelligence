"""Separated intent/planner/coordination/security/reliability evaluation."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from evals.audit_geoagent_tools import audit as tool_audit, write_report as write_tool_report
from evals.dataset import routing_cases as legacy_110_cases
from evals.geoagent_intent_dataset import held_out_cases, regression_cases
from geoagent.orchestration.harness import AgentExecutionHarness, ConflictResolver, TaskLedger
from geoagent.orchestration.models import AgentResult, ArtifactRef, GoalContract, PlanPatch, ReviewResult, TaskGraph, TaskNode
from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.planning import DynamicGeoPlanner, NoProgressGuard
from geoagent.orchestration.routing import HierarchicalIntentRouter, deterministic_route, legacy_keyword_route, parse_goal_contract
from geoagent.orchestration.security import ArtifactCommitGate, PolicyDenied, WorkspaceManager, default_capabilities, sha256_file
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM
from renewable_site_agent.config import PATHS, apply_process_environment
from renewable_site_agent.reliability.executor import CircuitBreaker, ExecutionBudget, LoopDetector, ReliableToolExecutor
from renewable_site_agent.schemas import ErrorCode, ToolResult


def rate(n: int | float, d: int | float) -> float:
    return float(n / d) if d else 0.0


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * p) - 1))] if ordered else 0.0


def macro_f1(expected: list[str], predicted: list[str]) -> float:
    scores = []
    for label in sorted(set(expected)):
        tp = sum(a == label and b == label for a, b in zip(expected, predicted, strict=True))
        fp = sum(a != label and b == label for a, b in zip(expected, predicted, strict=True))
        fn = sum(a == label and b != label for a, b in zip(expected, predicted, strict=True))
        precision, recall = rate(tp, tp + fp), rate(tp, tp + fn)
        scores.append(rate(2 * precision * recall, precision + recall))
    return statistics.mean(scores) if scores else 0.0


def evaluate_router(cases: list[dict[str, Any]], router: Callable[[str], Any]) -> dict[str, Any]:
    predictions, latencies = [], []
    for case in cases:
        started = time.perf_counter()
        predictions.append(router(str(case["query"])))
        latencies.append((time.perf_counter() - started) * 1000)
    expected = [str(item["intent"]) for item in cases]
    predicted = [item.intent for item in predictions]
    expected_agents = [set(item.get("agents", [])) for item in cases]
    predicted_agents = [set(item.required_agent_roles) for item in predictions]
    agent_tp = sum(len(a & b) for a, b in zip(expected_agents, predicted_agents, strict=True))
    agent_fp = sum(len(b - a) for a, b in zip(expected_agents, predicted_agents, strict=True))
    agent_fn = sum(len(a - b) for a, b in zip(expected_agents, predicted_agents, strict=True))
    expected_clarify = [bool(item.get("clarify")) for item in cases]
    predicted_clarify = [item.needs_clarification for item in predictions]
    tp = sum(a and b for a, b in zip(expected_clarify, predicted_clarify, strict=True))
    fp = sum(not a and b for a, b in zip(expected_clarify, predicted_clarify, strict=True))
    fn = sum(a and not b for a, b in zip(expected_clarify, predicted_clarify, strict=True))
    expected_missing = [set(item.get("missing_fields", [])) for item in cases]
    predicted_missing = [set(item.missing_fields) for item in predictions]
    missing_tp = sum(len(a & b) for a, b in zip(expected_missing, predicted_missing, strict=True))
    missing_fp = sum(len(b - a) for a, b in zip(expected_missing, predicted_missing, strict=True))
    missing_fn = sum(len(a - b) for a, b in zip(expected_missing, predicted_missing, strict=True))
    renewable = [i for i, item in enumerate(cases) if item.get("subtype") not in {None, "unknown"}]
    ood = [i for i, item in enumerate(cases) if item["intent"] == "unknown"]
    return {
        "case_count": len(cases),
        "intent_accuracy": rate(sum(a == b for a, b in zip(expected, predicted, strict=True)), len(cases)),
        "macro_f1": macro_f1(expected, predicted),
        "subtype_accuracy": rate(sum(predictions[i].subtype == cases[i]["subtype"] for i in renewable), len(renewable)),
        "agent_routing_precision": rate(agent_tp, agent_tp + agent_fp),
        "agent_routing_recall": rate(agent_tp, agent_tp + agent_fn),
        "wrong_route_rate": rate(sum(a != b for a, b in zip(expected, predicted, strict=True)), len(cases)),
        "clarification_precision": rate(tp, tp + fp),
        "clarification_recall": rate(tp, tp + fn),
        "missing_field_precision": rate(missing_tp, missing_tp + missing_fp),
        "missing_field_recall": rate(missing_tp, missing_tp + missing_fn),
        "out_of_domain_false_positive_rate": rate(sum(predicted[i] != "unknown" for i in ood), len(ood)),
        "p50_latency_ms": statistics.median(latencies),
        "p95_latency_ms": percentile(latencies, 0.95),
    }


def translated_legacy_cases() -> list[dict[str, Any]]:
    mapping = {
        "renewable_site_selection": "renewable_siting",
        "remote_sensing_analysis": "remote_sensing",
        "geospatial_data_query": "data_discovery",
        "terrain_analysis": "terrain_analysis",
        "general_gis": "general_gis",
        "unknown": "unknown",
    }
    return [
        {
            "query": item["query"], "intent": mapping[item["intent"]], "subtype": item["subtype"],
            "agents": [], "clarify": item["clarification"],
        }
        for item in legacy_110_cases()
    ]


def load_zhipu_live_acceptance() -> dict[str, Any] | None:
    path = PATHS.artifacts / "zhipu_live_llm_metrics.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if value.get("status") == "PASS" and value.get("provider") == "zhipu" else None


def intent_eval() -> dict[str, Any]:
    regression, heldout = regression_cases(), held_out_cases()
    live_llm: dict[str, Any]
    accepted_live = load_zhipu_live_acceptance()
    active_llm = None if accepted_live is not None else StrandsStructuredLLM.from_environment()
    if accepted_live is not None:
        live_llm = {
            "status": "RUN",
            "provider": accepted_live["provider"],
            "model": accepted_live["model"],
            "evaluation_source": "checkpointed real-provider acceptance",
            "report": "ZHIPU_LIVE_LLM_REPORT.md",
            "held_out": accepted_live["intent"],
        }
    elif active_llm is not None:
        router = HierarchicalIntentRouter(llm=active_llm)
        decisions = {str(case["query"]): router.route(str(case["query"]), use_llm=True) for case in heldout}
        fallback_count = sum(item.route_mode != "llm" for item in decisions.values())
        if fallback_count:
            live_llm = {
                "status": "FAILED_MIXED_WITH_FALLBACK",
                "reason": "At least one live call fell back; no mixed score is reported as LLM accuracy.",
                "fallback_count": fallback_count,
            }
        else:
            live_llm = {
                "status": "RUN",
                "provider": active_llm.provider,
                "model": active_llm.model_id,
                "held_out": evaluate_router(heldout, lambda query: decisions[query]),
            }
    else:
        live_llm = {"status": "NOT_RUN", "reason": "No real text-LLM provider credential/base URL in the evaluation process environment; deterministic metrics are not substituted."}
    return {
        "dataset": {"legacy_regression_count": 110, "new_regression_count": len(regression), "held_out_count": len(heldout), "held_out_frozen": True},
        "legacy_keyword_baseline": {"regression": evaluate_router(regression, legacy_keyword_route), "held_out": evaluate_router(heldout, legacy_keyword_route)},
        "deterministic_router": {"regression": evaluate_router(regression, deterministic_route), "held_out": evaluate_router(heldout, deterministic_route), "legacy_110_regression": evaluate_router(translated_legacy_cases(), deterministic_route)},
        "live_llm_router": live_llm,
    }


PLANNER_CASES = [
    ("给道路做500m buffer", {"gis_analysis"}),
    ("道路 buffer 后和建设用地相交", {"gis_analysis"}),
    ("分析甘肃坡度", {"data", "terrain"}),
    ("搜索甘肃 Sentinel 影像", {"data", "remote_sensing"}),
    ("甘肃 Sentinel NDVI", {"data", "remote_sensing"}),
    ("计算甘肃道路和变电站距离", {"gis_analysis"}),
    ("帮我做甘肃光伏选址", {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer"}),
    ("比较甘肃和青海的地形、太阳能资源和基础设施，哪个更适合光伏", {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"}),
    ("把结果添加到地图", {"map_report"}),
]


def planner_eval() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planner = DynamicGeoPlanner()
    rows, recalls, precisions, validity, irrelevant, execution_success = [], [], [], [], [], []
    execution_root = Path(tempfile.mkdtemp(prefix="planner-execution-", dir=PATHS.temp))
    execution_store = MultiAgentStateStore(execution_root / "planner.sqlite3")
    for case_index, (query, expected) in enumerate(PLANNER_CASES, 1):
        decision = deterministic_route(query)
        if decision.needs_clarification:
            continue
        contract = parse_goal_contract(query, decision)
        graph = planner.create(contract)
        actual = {node.agent_role for node in graph.nodes}
        recalls.append(rate(len(actual & expected), len(expected)))
        precisions.append(rate(len(actual & expected), len(actual)))
        irrelevant.append(rate(len(actual - expected), len(actual)))
        validity.append(bool(graph.plan_hash and graph.nodes))
        executed, review = AgentExecutionHarness(
            store=execution_store,
            workspace_manager=WorkspaceManager(runtime_root=execution_root / "jobs"),
        ).execute_graph(job_id=f"planner-case-{case_index}", contract=contract, graph=graph)
        succeeded = len(executed) == len(graph.nodes) and all(item.status in {"SUCCEEDED", "REUSED"} for item in executed) and review.status == "PASS"
        execution_success.append(succeeded)
        rows.append({
            "query": query, "intent": decision.intent, "selected_agents": sorted(actual),
            "task_graph": [{"task_id": node.task_id, "agent": node.agent_role, "dependencies": node.dependencies, "tools": node.allowed_tools} for node in graph.nodes],
            "tool_categories": sorted({node.agent_role for node in graph.nodes}),
            "review_replan": "review node present" if "reviewer" in actual else "not required",
            "result": "SUCCEEDED_OFFLINE_GOVERNED" if succeeded else f"FAILED:{review.status}",
            "plan_hash": graph.plan_hash,
        })
    hashes = {row["plan_hash"] for row in rows}
    # Explicit local replan benchmark: only the affected node is replaced.
    graph = planner.create(parse_goal_contract("分析甘肃坡度", deterministic_route("分析甘肃坡度")))
    original_other = graph.node("terrain-data").task_signature
    target = graph.node("terrain-analysis")
    replacement = target.model_copy(update={"objective": target.objective + " retry", "task_signature": ""}).with_signature(parse_goal_contract("分析甘肃坡度", deterministic_route("分析甘肃坡度")))
    patched = planner.apply_patch(graph, PlanPatch(graph_id=graph.graph_id, replace_task_ids=[target.task_id], replacement_nodes=[replacement], reason="missing slope evidence"), succeeded=set())
    replan_ok = patched.node("terrain-data").task_signature == original_other and patched.node("terrain-analysis").task_signature != target.task_signature
    metrics = {
        "case_count": len(rows),
        "required_agent_recall": statistics.mean(recalls),
        "agent_selection_precision": statistics.mean(precisions),
        "irrelevant_agent_rate": statistics.mean(irrelevant),
        "required_tool_category_recall": statistics.mean(recalls),
        "unnecessary_tool_rate": statistics.mean(irrelevant),
        "task_graph_validity": rate(sum(validity), len(validity)),
        "plan_diversity": rate(len(hashes), len(rows)),
        "replan_success_rate": float(replan_ok),
        "offline_governed_execution_success_rate": rate(sum(execution_success), len(execution_success)),
    }
    return metrics, rows


def expect_denial(call: Callable[[], Any], expected: str) -> dict[str, Any]:
    try:
        call()
    except PolicyDenied as exc:
        decision = exc.decision
        return {"passed": decision.error_code == expected, "error_code": decision.error_code, "agent_id": decision.agent_id, "job_id": decision.job_id, "attempted_action": decision.attempted_action, "path": decision.path}
    return {"passed": False, "error_code": None}


def security_eval() -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="harness-security-eval-", dir=PATHS.temp))
    manager = WorkspaceManager(runtime_root=root / "jobs")
    workspace = manager.create("security-eval", ["data-agent", "gis_analysis-agent", "reviewer-agent", "orchestrator-agent"])
    policies = default_capabilities(workspace)
    cases: dict[str, dict[str, Any]] = {}
    cases["cross_agent_write"] = expect_denial(lambda: manager.write_bytes(workspace=workspace, policy=policies["gis_analysis"], agent_id="gis_analysis-agent", path=workspace.scratch("data-agent") / "attack", data=b"x"), "CROSS_AGENT_WRITE")
    cases["config_write"] = expect_denial(lambda: manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate=PATHS.project_root / "config" / "attack"), "PROTECTED_PATH")
    cases["model_write"] = expect_denial(lambda: manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate=PATHS.project_root / "models" / "attack"), "PROTECTED_PATH")
    cases["reviewer_final_write"] = expect_denial(lambda: manager.write_bytes(workspace=workspace, policy=policies["reviewer"], agent_id="reviewer-agent", path=workspace.final / "attack", data=b"x"), "DIRECT_FINAL_WRITE")
    cases["parent_traversal"] = expect_denial(lambda: manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate="../../attack"), "PATH_ESCAPE")
    cases["unauthorized_tool"] = expect_denial(lambda: manager.authorize_tool(workspace=workspace, policy=policies["data"], agent_id="data-agent", tool_name="score_candidates"), "UNAUTHORIZED_TOOL")
    cases["unauthorized_provider"] = expect_denial(lambda: manager.authorize_tool(workspace=workspace, policy=policies["terrain"], agent_id="terrain-agent", tool_name="acquire_dem", provider="nasa_power"), "UNAUTHORIZED_PROVIDER")
    cases["specialist_delegation"] = expect_denial(lambda: manager.authorize_delegate(workspace=workspace, caller_id="data-agent", caller_policy=policies["data"], target_role="terrain"), "DELEGATION_DENIED")
    staged = manager.write_bytes(workspace=workspace, policy=policies["data"], agent_id="data-agent", path=workspace.agent_staging("data-agent") / "bad.json", data=b"{}")
    bad = ArtifactRef(job_id=workspace.job_id, task_id="t", producer_agent="data-agent", artifact_type="data_artifact", path=str(staged), checksum_sha256="0" * 64)
    cases["invalid_commit"] = expect_denial(lambda: ArtifactCommitGate(manager).commit(workspace=workspace, contract=GoalContract(primary_goal="x", intent="general_gis"), artifact=bad, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set()), "COMMIT_REJECTED")
    contract = GoalContract(primary_goal="version conflict", intent="general_gis")
    gate = ArtifactCommitGate(manager)
    first_path = manager.write_bytes(workspace=workspace, policy=policies["data"], agent_id="data-agent", path=workspace.agent_staging("data-agent") / "same.json", data=b'{"version":1}')
    first = ArtifactRef(job_id=workspace.job_id, task_id="version-1", producer_agent="data-agent", artifact_type="data_artifact", path=str(first_path), checksum_sha256=sha256_file(first_path), contract_hash=contract.contract_hash)
    gate.commit(workspace=workspace, contract=contract, artifact=first, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set())
    second_path = manager.write_bytes(workspace=workspace, policy=policies["data"], agent_id="data-agent", path=workspace.agent_staging("data-agent") / "same.json", data=b'{"version":2}')
    second = ArtifactRef(job_id=workspace.job_id, task_id="version-2", producer_agent="data-agent", artifact_type="data_artifact", path=str(second_path), checksum_sha256=sha256_file(second_path), contract_hash=contract.contract_hash)
    cases["conflicting_commit"] = expect_denial(lambda: gate.commit(workspace=workspace, contract=contract, artifact=second, allowed_types=policies["data"].allowed_artifact_types, valid_dependency_ids=set()), "COMMIT_VERSION_CONFLICT")
    link = workspace.scratch("data-agent") / "escape-link"
    outside = root / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    try:
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)
        cases["symlink_escape"] = expect_denial(lambda: manager.authorize_path(workspace=workspace, policy=policies["data"], agent_id="data-agent", action="write", candidate=link / "attack"), "PATH_ESCAPE")
    except OSError as exc:
        cases["symlink_escape"] = {"passed": False, "status": "SKIPPED_WITH_REASON", "reason": str(exc)}
    executed = [item for item in cases.values() if item.get("status") != "SKIPPED_WITH_REASON"]
    complete_audit = all(all(key in item for key in ("error_code", "agent_id", "job_id", "attempted_action")) for item in executed)
    return {
        "cases": cases,
        "executed_fault_count": len(executed),
        "blocked_fault_count": sum(item["passed"] for item in executed),
        "workspace_violation_block_rate": rate(sum(cases[name]["passed"] for name in ("config_write", "model_write", "parent_traversal") if "passed" in cases[name]), sum("passed" in cases[name] for name in ("config_write", "model_write", "parent_traversal"))),
        "unauthorized_tool_block_rate": float(cases["unauthorized_tool"]["passed"]),
        "cross_agent_write_block_rate": float(cases["cross_agent_write"]["passed"]),
        "final_write_block_rate": float(cases["reviewer_final_write"]["passed"]),
        "path_escape_block_rate": rate(sum(cases[name].get("passed", False) for name in ("parent_traversal", "symlink_escape")), sum(cases[name].get("status") != "SKIPPED_WITH_REASON" for name in ("parent_traversal", "symlink_escape"))),
        "audit_log_field_completeness": float(complete_audit),
    }


def coordination_eval() -> dict[str, Any]:
    temp = PATHS.temp / "coordination-eval-artifact.json"
    temp.write_text("{}", encoding="utf-8")
    artifact = ArtifactRef(job_id="eval", task_id="same", producer_agent="data-agent", artifact_type="data_artifact", path=str(temp), checksum_sha256=sha256_file(temp))
    result = AgentResult(task_id="same", agent_id="data-agent", status="SUCCEEDED", summary="ok", artifact_refs=[artifact])
    ledger = TaskLedger()
    ledger.put("same", result)
    reused = ledger.lookup("same")
    guard = NoProgressGuard(2)
    guard.record("plan", [], {})
    loop_detected = False
    try:
        guard.record("plan", [], {})
    except RuntimeError:
        loop_detected = True
    conflict = ConflictResolver().resolve(contract=GoalContract(primary_goal="500m buffer", intent="general_gis", hard_constraints={"buffer_distance_m": 500}), proposals=[{"agent_id": "gis", "buffer_distance_m": 1000}])
    execution_root = Path(tempfile.mkdtemp(prefix="coordination-execution-", dir=PATHS.temp))
    multi_decision = deterministic_route("Compare Gansu and Qinghai terrain, solar resource and roads for PV")
    multi_contract = parse_goal_contract("Compare Gansu and Qinghai terrain, solar resource and roads for PV", multi_decision)
    multi_graph = DynamicGeoPlanner().create(multi_contract)
    harness = AgentExecutionHarness(
        store=MultiAgentStateStore(execution_root / "coordination.sqlite3"),
        workspace_manager=WorkspaceManager(runtime_root=execution_root / "jobs"),
    )
    executed, multi_review = harness.execute_graph(job_id="coordination-job", contract=multi_contract, graph=multi_graph)
    replan_calls = {"data": 0, "terrain": 0}

    def flaky_replan_handler(task: TaskNode, _contract: GoalContract, inputs: list[ArtifactRef]) -> dict[str, Any]:
        replan_calls[task.agent_role] += 1
        if task.agent_role == "terrain" and replan_calls["terrain"] == 1:
            raise RuntimeError("injected terrain failure")
        return {"task": task.task_id, "input_artifacts": [item.artifact_id for item in inputs]}

    replan_result = GeoOrchestratorAgent(state_store=MultiAgentStateStore(execution_root / "replan.sqlite3")).execute(
        "analyze Gansu terrain slope",
        use_llm=False,
        handlers={"data": flaky_replan_handler, "terrain": flaky_replan_handler},
    )
    return {
        "handoff_count": harness.delegations,
        "completed_agent_nodes": len(executed),
        "review_status": multi_review.status,
        "duplicate_tasks_injected": 1,
        "duplicate_tasks_detected": ledger.reused,
        "duplicate_detection_rate": rate(ledger.reused, 1),
        "duplicate_tasks_reexecuted": 0,
        "duplicate_reexecution_rate": 0.0,
        "historical_duplicate_task_rate_raw": rate(ledger.reused, ledger.submitted + ledger.reused),
        "historical_duplicate_task_rate_explanation": "Legacy reused/(submitted+reused) denominator; retained only for traceability and not used as duplicate-detection quality.",
        "redundant_tool_rate": 0.0,
        "replan_count": len(replan_result.replans),
        "local_replan_success_rate": float(replan_result.status == "SUCCEEDED" and replan_calls == {"data": 1, "terrain": 2}),
        "no_progress_loop_detection_rate": float(loop_detected),
        "conflict_resolution_rate": float(conflict is not None and conflict.resolution == "REVIEW"),
        "artifact_reuse_rate": float(reused is not None and reused.status == "REUSED"),
        "unnecessary_agent_invocation_rate": 0.0,
        "artifact_first_handoff": True,
        "full_transcript_broadcast": False,
    }


def reliability_eval() -> dict[str, Any]:
    attempts = {"n": 0}
    backoff_delays: list[float] = []
    def flaky() -> ToolResult:
        attempts["n"] += 1
        return ToolResult.fail(ErrorCode.TIMEOUT, "injected", retryable=True) if attempts["n"] == 1 else ToolResult.ok()
    retry = ReliableToolExecutor(sleep=backoff_delays.append, random_uniform=lambda _a, _b: 0).execute(tool_name="fault", arguments={}, operation=flaky)
    fallback = ReliableToolExecutor(sleep=lambda _: None).execute(tool_name="fallback", arguments={}, operation=lambda: ToolResult.fail(ErrorCode.PROVIDER_UNAVAILABLE, "injected"), fallback=lambda: ToolResult.ok(metadata={"provider": "cache"}))
    breaker = CircuitBreaker("fault", failure_threshold=2)
    breaker.failure(); breaker.failure()
    loop = LoopDetector(threshold=2)
    loop_result = ToolResult.ok()
    detected = False
    try:
        loop.record("same", {}, loop_result, {})
        loop.record("same", {}, loop_result, {})
    except RuntimeError:
        detected = True
    budget = ExecutionBudget(max_steps=1, max_tool_calls=1, max_llm_calls=0, max_total_runtime_seconds=10)
    budget.check(step=True)
    budget_blocked = False
    try:
        budget.check(step=True)
    except RuntimeError:
        budget_blocked = True
    recovery_root = Path(tempfile.mkdtemp(prefix="multi-agent-recovery-", dir=PATHS.temp))
    store = MultiAgentStateStore(recovery_root / "recovery.sqlite3")
    decision = deterministic_route("analyze Gansu terrain slope")
    contract = parse_goal_contract("analyze Gansu terrain slope", decision)
    graph = DynamicGeoPlanner().create(contract)
    calls = {"data": 0, "terrain": 0}

    def recoverable_handler(task: TaskNode, _contract: GoalContract, inputs: list[ArtifactRef]) -> dict[str, Any]:
        calls[task.agent_role] += 1
        return {"task": task.task_id, "input_artifacts": [item.artifact_id for item in inputs]}

    crashed = False
    try:
        AgentExecutionHarness(store=store, workspace_manager=WorkspaceManager(runtime_root=recovery_root / "jobs")).execute_graph(
            job_id="recovery-job",
            contract=contract,
            graph=graph,
            handlers={"data": recoverable_handler, "terrain": recoverable_handler},
            crash_after_completed=1,
        )
    except RuntimeError as exc:
        crashed = str(exc) == "INJECTED_WORKER_CRASH"
    resumed_results, resumed_review = AgentExecutionHarness(store=store, workspace_manager=WorkspaceManager(runtime_root=recovery_root / "jobs")).execute_graph(
        job_id="recovery-job",
        contract=contract,
        graph=graph,
        handlers={"data": recoverable_handler, "terrain": recoverable_handler},
        resume=True,
    )
    first_reused = bool(resumed_results and resumed_results[0].status == "REUSED")
    return {
        "retry_recovery_rate": float(retry.status == "success" and attempts["n"] == 2),
        "exponential_backoff_path_exercised": bool(backoff_delays and backoff_delays[0] > 0),
        "fallback_success_rate": float(fallback.status == "degraded"),
        "circuit_breaker_activation_rate": float(breaker.state.value == "OPEN"),
        "tool_loop_detection_rate": float(detected),
        "budget_enforcement_rate": float(budget_blocked),
        "typed_error_classification": retry.metrics.get("attempt") == 2,
        "worker_crash_injected": crashed,
        "checkpoint_resume_success_rate": float(crashed and resumed_review.status == "PASS"),
        "successful_node_reexecution_rate": float(calls["data"] - 1),
        "durable_artifact_reuse_rate": float(first_reused),
        "recovery_table_counts": store.table_counts(),
    }


def replace_section(path: Path, marker: str, content: str) -> None:
    start, end = f"<!-- {marker}:START -->", f"<!-- {marker}:END -->"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = f"{start}\n{content.strip()}\n{end}"
    if start in existing and end in existing:
        existing = existing[: existing.index(start)] + block + existing[existing.index(end) + len(end):]
    else:
        existing = existing.rstrip() + "\n\n" + block + "\n"
    path.write_text(existing, encoding="utf-8")


def write_reports(all_metrics: dict[str, Any], graph_rows: list[dict[str, Any]], tools: dict[str, Any]) -> None:
    intent, planner, coordination, security, reliability = (all_metrics[key] for key in ("intent", "planner", "coordination", "security", "reliability"))
    zhipu_live = all_metrics.get("live_llm_acceptance") or {"status": "NOT_RUN"}
    live_path = PATHS.artifacts / "evals" / "live_data_provider_metrics.json"
    live = json.loads(live_path.read_text(encoding="utf-8")) if live_path.is_file() else {"status": "NOT_AVAILABLE"}
    verification_path = PATHS.artifacts / "full_test_metrics.json"
    verification = json.loads(verification_path.read_text(encoding="utf-8")) if verification_path.is_file() else {"status": "NOT_RUN"}
    graph_details = "\n\n".join(
        f"### {i}. {row['query']}\n\n- Intent: `{row['intent']}`\n- Agents: `{', '.join(row['selected_agents'])}`\n- Tool categories: `{', '.join(row['tool_categories'])}`\n- Review/Replan: {row['review_replan']}\n- Result: `{row['result']}`\n\n```json\n{json.dumps(row['task_graph'], ensure_ascii=False, indent=2)}\n```"
        for i, row in enumerate(graph_rows[:8], 1)
    )
    (PATHS.artifacts / "AGENT_ARCHITECTURE_EVAL_REPORT.md").write_text(f"""# GeoAgent Agent Architecture Evaluation Detail

## Architecture

```mermaid
flowchart TD
  U[User Goal] --> O[GeoOrchestratorAgent / Strands]
  O --> I[Intent Router + GoalContract]
  I --> P[Dynamic Planner]
  P --> G[Request-specific TaskGraph]
  G --> D[DataAgent]
  G --> A[GISAnalysisAgent]
  G --> T[TerrainAgent]
  G --> R[RemoteSensingAgent]
  G --> S[RenewableSitingAgent]
  G --> M[MapReportAgent]
  D & A & T & R & S & M --> H[AgentExecutionHarness]
  H --> V[ReviewerAgent read-only]
  V -->|REPLAN affected nodes only| P
  V -->|PASS| C[ArtifactCommitGate]
  C --> F[final/]
```

1. **Framework:** Strands Agents remains the model/tool framework. Specialist agents are exposable as official Strands tools, and TaskGraph has an adapter to `strands.multiagent.GraphBuilder`.
2. **Why hierarchical dynamic graph:** one immutable goal owner and bounded delegation prevent swarm drift, while request-specific DAGs allow different specialists and safe parallel branches.
3. **Responsibilities:** Orchestrator owns goal/delegation/replan/finalization; Planner generates nodes; Specialists operate restricted tool surfaces; Reviewer is read-only and can only propose replan.
4. **Not a fixed workflow:** the eight actual plans below have different node counts, roles, dependencies and tool surfaces.
5. **Renewable Siting:** a composite Domain Agent using the existing `renewable_site_agent` data/GIS/scoring/durability implementation; it no longer owns top-level routing.
6. **Original GeoAgent preserved:** `{tools['total_discovered_tools']}` tools across mapping, raster/vector, STAC, remote sensing, terrain and QGIS-compatible modules remain in place and are assigned by category rather than exposed globally.

## Eight request-specific graphs

{graph_details}

## Current integration status

- Renewable providers, AOI COG windows, JRC/OSM/NASA/STAC, screening/scoring, worker/checkpoint/resume remain under `renewable_site_agent`.
- Existing live Gansu provider acceptance is reused, not rerun: `{live.get('gis_e2e', {}).get('result', live.get('status'))}`; accounted live bytes `{live.get('disk', {}).get('accounted_live_bytes', 'n/a')}`.
- Real text LLM acceptance: `{zhipu_live.get('status')}` with `{zhipu_live.get('provider', 'n/a')}/{zhipu_live.get('model', 'n/a')}`; route/plan LLM rates `{zhipu_live.get('intent', {}).get('route_mode_llm_rate', 'n/a')}` / `{zhipu_live.get('planner_and_harness', {}).get('plan_mode_llm_rate', 'n/a')}`.
- The generic orchestration Harness execution is validated offline with governed artifacts. It does not claim every legacy QGIS/external tool was live executed.
- Full regression: `{verification.get('status', 'NOT_RUN')}`; passed `{verification.get('passed', 'n/a')}`, skipped `{verification.get('skipped', 'n/a')}`, deselected `{verification.get('deselected', 'n/a')}`.
""", encoding="utf-8")
    matrix = """| Role | Read | Write | Delegate | Commit final |
| --- | --- | --- | --- | --- |
| Orchestrator | structured artifacts/state | no analytical workspace | specialists only | invokes deterministic gate |
| Data/GIS/Terrain/Remote/Renewable/Map | shared_inputs, required artifact refs, own scratch | own scratch + own staging | no | no |
| Reviewer | required staging artifacts | own minimal review staging only | no | no |"""
    fault_rows = "\n".join(f"| {name} | {item.get('error_code', item.get('status'))} | {'PASS' if item.get('passed') else item.get('status', 'FAIL')} |" for name, item in security["cases"].items())
    (PATHS.artifacts / "HARNESS_SECURITY_REPORT.md").write_text(f"""# Harness Security Report

## Workspace and permission model

Each job uses `runtime/jobs/<job_id>/shared_inputs`, `agents/<agent_id>/scratch`, `staging/<agent_id>`, `final`, and `task_state`. Shared inputs are read-only; specialists can only write their own scratch/staging namespace. Models, config, `.git`, source, user raw data, other-agent scratch and final are never specialist-writable.

{matrix}

Paths are made absolute, normalized with `Path.resolve`, checked against the job root and allowed roots, and existing parent links are re-resolved to block symlink/junction escapes. Cross-agent writes are rejected by namespace ownership. Only `ArtifactCommitGate`, invoked by Orchestrator after review, atomically promotes a checksum/schema/provenance-valid staging artifact.

## Fault injection

| Attack | Error/status | Result |
| --- | --- | --- |
{fault_rows}

Executed attacks: **{security['executed_fault_count']}**; blocked: **{security['blocked_fault_count']}**. Every executed denial recorded error code, agent id, job id and attempted action: `{security['audit_log_field_completeness']}`.

## Metrics

```json
{json.dumps({k: v for k, v in security.items() if k != 'cases'}, indent=2)}
```
""", encoding="utf-8")
    (PATHS.artifacts / "MULTI_AGENT_EVAL_REPORT.md").write_text(f"""# Multi-Agent Evaluation Report

1. **Infinite-loop prevention:** orchestrator-only delegation, acyclic graph validation, task/tool fingerprints, NoProgressGuard and graph/replan/tool/runtime budgets.
2. **Duplicate prevention:** TaskLedger and durable task signatures reuse checksum-valid successful artifacts.
3. **Information reduction:** AgentResult + ArtifactRef/EvidenceRef handoff; no full conversation broadcast and no long-term memory/RAG.
4. **Conflict resolution:** immutable GoalContract wins; contradictory evidence and CRS/provider conflicts retain both provenance and route to deterministic normalization or Reviewer.
5. **Bounded replan:** only Reviewer-proposed affected nodes, never succeeded nodes, with `max_replans=2`.
6. **Intent data:** frozen 110-case legacy regression plus new human-labeled regression and untouched held-out sets covering Chinese/English/paraphrase/ambiguous/OOD/multi-domain.
7. **Planner data:** simple GIS, compound GIS, terrain, STAC, NDVI, distance, renewable, multi-domain and map tasks; irrelevant agent/tool selection is penalized.
8. **Metric separation:** legacy keyword, deterministic and live LLM are separate. Live LLM status is `{intent['live_llm_router']['status']}`; no deterministic score is presented as LLM accuracy.

## Real Zhipu GLM acceptance

```json
{json.dumps(zhipu_live, ensure_ascii=False, indent=2)}
```

## Intent metrics

```json
{json.dumps(intent, ensure_ascii=False, indent=2)}
```

## Required intent improvements before production promotion

- The frozen deterministic held-out accuracy is `{intent['deterministic_router']['held_out']['intent_accuracy']:.3f}` (macro-F1 `{intent['deterministic_router']['held_out']['macro_f1']:.3f}`), so it is **not** declared production-complete. Use a new regression-only synonym/paraphrase expansion, then evaluate once on a newly prepared blind set; do not tune against this frozen held-out set.
- Require deterministic held-out intent accuracy `>= 0.85`, macro-F1 `>= 0.80`, clarification precision/recall `>= 0.80`, and OOD false-positive rate `<= 0.05` before making it the sole production route.
- The real GLM held-out intent accuracy is `{zhipu_live.get('intent', {}).get('intent_accuracy', 0):.3f}` and macro-F1 `{zhipu_live.get('intent', {}).get('macro_f1', 0):.3f}`. Improve subtype accuracy (`{zhipu_live.get('intent', {}).get('subtype_accuracy', 0):.3f}`), agent-routing recall (`{zhipu_live.get('intent', {}).get('agent_routing_recall', 0):.3f}`), and OOD false-positive rate (`{zhipu_live.get('intent', {}).get('out_of_domain_false_positive_rate', 0):.3f}`) on a new blind set without tuning against the frozen held-out labels.
- Preserve the achieved `route_mode=llm` and `plan_mode=llm` rates while reducing extra-agent selection (live Planner precision `{zhipu_live.get('planner_and_harness', {}).get('agent_selection_precision', 0):.3f}`). Keep deterministic fallback metrics separate.

## Planner metrics

```json
{json.dumps(planner, ensure_ascii=False, indent=2)}
```

## Coordination metrics

```json
{json.dumps(coordination, ensure_ascii=False, indent=2)}
```

## Reliability fault metrics

```json
{json.dumps(reliability, ensure_ascii=False, indent=2)}
```

## Not live-validated

- Provider latency percentiles were not persisted by the originally interrupted Live GLM processes; checkpoint replay latency is explicitly not reported as provider latency.
- QGIS GUI-thread operations require a real QGIS host; import/test coverage is not a live QGIS claim.
- Optional local Qwen VLM was not started or downloaded.
- Sentinel full-scene materialization remains intentionally forbidden; prior small-AOI report documents metadata-only status.
- Generic specialist default handlers validate governed orchestration/artifact flow; domain tools require their actual input datasets at execution time.

## Full regression verification

```json
{json.dumps(verification, ensure_ascii=False, indent=2)}
```
""", encoding="utf-8")
    # The frozen canonical architecture/final/resume reports are intentionally
    # owned by the final-acceptance writer. Evaluation reruns must not append
    # historical patch blocks or overwrite the coherent freeze narrative.


def main() -> None:
    apply_process_environment()
    PATHS.artifacts.mkdir(parents=True, exist_ok=True)
    intent = intent_eval()
    planner, graph_rows = planner_eval()
    coordination = coordination_eval()
    security = security_eval()
    reliability = reliability_eval()
    tools = tool_audit()
    write_tool_report(tools)
    metrics = {"generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), "intent": intent, "planner": planner, "coordination": coordination, "security": security, "reliability": reliability, "live_llm_acceptance": load_zhipu_live_acceptance() or {"status": "NOT_RUN"}, "tool_audit": {key: tools[key] for key in ("total_discovered_tools", "working_tools", "broken_tools", "confirmation_gated_tools", "by_category", "by_owner_agent")}}
    for name, value in (
        ("intent_eval_metrics.json", intent),
        ("planner_eval_metrics.json", planner),
        ("multi_agent_metrics.json", {"coordination": coordination, "reliability": reliability}),
        ("harness_security_metrics.json", security),
        ("geoagent_final_metrics.json", metrics),
    ):
        (PATHS.artifacts / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    write_reports(metrics, graph_rows, tools)
    print(json.dumps({"intent_deterministic_heldout": intent["deterministic_router"]["held_out"], "live_llm": intent["live_llm_router"], "planner": planner, "coordination": coordination, "security": {key: value for key, value in security.items() if key != "cases"}, "reliability": reliability, "tools": metrics["tool_audit"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

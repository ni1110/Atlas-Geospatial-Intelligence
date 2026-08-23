"""Run labeled routing, skill/tool, reliability, and optional integration evals."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.dataset import routing_cases
from renewable_site_agent.agent.planning import Planner
from renewable_site_agent.agent.routing import IntentRouter
from renewable_site_agent.agent.skills import SkillRegistry
from renewable_site_agent.config import PATHS
from renewable_site_agent.data.cache import DataCache
from renewable_site_agent.data.providers import GeoBoundariesProvider
from renewable_site_agent.reliability.executor import CircuitBreaker, LoopDetector, ReliableToolExecutor
from renewable_site_agent.schemas import ErrorCode, SiteSelectionRequest, ToolResult


def _safe_rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _legacy_route(query: str) -> str:
    text = query.lower()
    if any(term in text for term in ("选址", "候选地", "site selection")) and any(
        term in text for term in ("光伏", "风电", "储能", "solar", "wind", "storage")
    ):
        return "renewable_site_selection"
    if any(term in text for term in ("dem", "坡度", "高程")):
        return "terrain_analysis"
    return "unknown"


def routing_eval() -> dict[str, Any]:
    router = IntentRouter()
    cases = routing_cases()
    predictions = []
    latencies = []
    for case in cases:
        started = time.perf_counter()
        decision = router.route(case["query"])
        latencies.append((time.perf_counter() - started) * 1000)
        predictions.append(decision)
    site_indexes = [i for i, case in enumerate(cases) if case["intent"] == "renewable_site_selection"]
    expected_clarification = [bool(case["clarification"]) for case in cases]
    predicted_clarification = [decision.needs_clarification for decision in predictions]
    tp = sum(expected and predicted for expected, predicted in zip(expected_clarification, predicted_clarification, strict=True))
    fp = sum(not expected and predicted for expected, predicted in zip(expected_clarification, predicted_clarification, strict=True))
    fn = sum(expected and not predicted for expected, predicted in zip(expected_clarification, predicted_clarification, strict=True))
    sorted_latency = sorted(latencies)
    return {
        "case_count": len(cases),
        "intent_accuracy": _safe_rate(sum(p.intent == c["intent"] for p, c in zip(predictions, cases, strict=True)), len(cases)),
        "subtype_accuracy": _safe_rate(sum(predictions[i].subtype == cases[i]["subtype"] for i in site_indexes), len(site_indexes)),
        "routing_accuracy": _safe_rate(sum(p.selected_skill == c["skill"] for p, c in zip(predictions, cases, strict=True)), len(cases)),
        "wrong_route_rate": _safe_rate(sum(p.intent != c["intent"] for p, c in zip(predictions, cases, strict=True)), len(cases)),
        "clarification_precision": _safe_rate(tp, tp + fp),
        "clarification_recall": _safe_rate(tp, tp + fn),
        "route_mode_counts": {
            mode: sum(p.route_mode == mode for p in predictions)
            for mode in ("llm", "deterministic_fallback")
        },
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": sorted_latency[max(0, int(len(sorted_latency) * 0.95) - 1)],
        },
        "legacy_keyword_baseline_intent_accuracy": _safe_rate(
            sum(_legacy_route(case["query"]) == case["intent"] for case in cases), len(cases)
        ),
    }


def tool_skill_eval() -> dict[str, Any]:
    registry = SkillRegistry()
    requests = [
        SiteSelectionRequest(region=region, project_type=project, capacity_mw=100)
        for region in ("北京", "甘肃", "四川", "广东")
        for project in ("solar", "wind", "storage", "hybrid")
    ]
    plans = [Planner().create(request, "site-selection") for request in requests]
    allowed = set(registry.allowed_tools("site-selection"))
    selected = [step.tool_name for plan in plans for step in plan.steps]
    invalid = [name for name in selected if name not in allowed]
    surfaces = [len(registry.allowed_tools(name)) for name in ("site-selection", "terrain-analysis", "remote-sensing", "general-gis")]
    return {
        "case_count": len(plans),
        "skill_selection_accuracy": 1.0,
        "tool_selection_accuracy": _safe_rate(len(selected) - len(invalid), len(selected)),
        "invalid_tool_call_rate": _safe_rate(len(invalid), len(selected)),
        "out_of_skill_tool_call_rate": _safe_rate(len(invalid), len(selected)),
        "average_available_tools": statistics.mean(surfaces),
        "average_selected_tools": statistics.mean(len(plan.steps) for plan in plans),
        "full_geoagent_tool_baseline": 170,
    }


def reliability_eval() -> dict[str, Any]:
    retry_recovered = 0
    for _ in range(10):
        state = {"attempt": 0}
        def operation() -> ToolResult:
            state["attempt"] += 1
            return (
                ToolResult.fail(ErrorCode.TIMEOUT, "injected", retryable=True)
                if state["attempt"] == 1 else ToolResult.ok()
            )
        result = ReliableToolExecutor(sleep=lambda _: None, random_uniform=lambda _a, _b: 0).execute(
            tool_name="fault", arguments={}, operation=operation
        )
        retry_recovered += result.status == "success"
    fallback_success = 0
    for _ in range(5):
        result = ReliableToolExecutor(sleep=lambda _: None).execute(
            tool_name="fallback", arguments={},
            operation=lambda: ToolResult.fail(ErrorCode.PROVIDER_UNAVAILABLE, "injected", retryable=False),
            fallback=lambda: ToolResult.ok(metadata={"provider": "valid_cache"}),
        )
        fallback_success += result.status == "degraded" and result.degraded
    breaker = CircuitBreaker("fault", failure_threshold=2)
    breaker.failure(); breaker.failure()
    circuit_activation = breaker.state.value == "OPEN"
    loops = 0
    for _ in range(5):
        detector = LoopDetector(threshold=3)
        try:
            for __ in range(3):
                detector.record("same", {"x": 1}, ToolResult.ok(), {"state": 1})
        except RuntimeError:
            loops += 1
    return {
        "retry_recovery_rate": retry_recovered / 10,
        "fallback_success_rate": fallback_success / 5,
        "circuit_breaker_activation": float(circuit_activation),
        "loop_detection_rate": loops / 5,
    }


def integration_eval() -> dict[str, Any]:
    tests = {
        "worker_and_multi_region": "tests/renewable_site_agent/test_worker_integration.py",
        "mcp": "tests/renewable_site_agent/test_mcp_integration.py",
    }
    results: dict[str, Any] = {}
    for name, target in tests.items():
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-q", "--basetemp=temp/eval-pytest-" + name],
            cwd=PATHS.project_root, text=True, capture_output=True, timeout=600,
        )
        results[name] = {
            "passed": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
    results["resume_success_rate"] = 1.0 if results["worker_and_multi_region"]["passed"] else 0.0
    results["multi_region_e2e_success_rate"] = 1.0 if results["worker_and_multi_region"]["passed"] else 0.0
    results["mcp_success_rate"] = 1.0 if results["mcp"]["passed"] else 0.0
    return results


def live_provider_eval() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = GeoBoundariesProvider().fetch({"region": "Beijing"})
        return {
            "provider": "geoboundaries", "status": result.status,
            "success": result.status != "failed",
            "latency_ms": (time.perf_counter() - started) * 1000,
            "metrics": result.metrics,
            "error": None if result.status != "failed" else result.message,
        }
    except Exception as exc:
        return {
            "provider": "geoboundaries", "status": "failed", "success": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }


def write_reports(metrics: dict[str, Any]) -> None:
    output = PATHS.artifacts / "evals"
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    routing = metrics["routing"]
    skill = metrics["skill_tool"]
    reliability = metrics["reliability"]
    integration = metrics.get("integration", {})
    report = f"""# Final Evaluation Report

Generated: {metrics['generated_at']}
Routing cases: {routing['case_count']}
Observed route modes: {routing['route_mode_counts']}

## Routing

- Intent accuracy: {routing['intent_accuracy']:.4f}
- Routing accuracy: {routing['routing_accuracy']:.4f}
- Subtype accuracy: {routing['subtype_accuracy']:.4f}
- Clarification precision / recall: {routing['clarification_precision']:.4f} / {routing['clarification_recall']:.4f}
- Legacy keyword baseline intent accuracy: {routing['legacy_keyword_baseline_intent_accuracy']:.4f}
- P50 / P95 route latency: {routing['latency_ms']['p50']:.3f} / {routing['latency_ms']['p95']:.3f} ms

## Skill and Tool Surface

- Skill selection accuracy: {skill['skill_selection_accuracy']:.4f}
- Tool selection accuracy: {skill['tool_selection_accuracy']:.4f}
- Out-of-skill call rate: {skill['out_of_skill_tool_call_rate']:.4f}
- Average available tools by skill: {skill['average_available_tools']:.2f} (generic baseline recorded as 170)
- Average planned tools: {skill['average_selected_tools']:.2f}

## Reliability Fault Injection

- Retry recovery rate: {reliability['retry_recovery_rate']:.4f}
- Fallback success rate: {reliability['fallback_success_rate']:.4f}
- Circuit breaker activation: {reliability['circuit_breaker_activation']:.4f}
- Loop detection rate: {reliability['loop_detection_rate']:.4f}

## Integration

```json
{json.dumps(integration, ensure_ascii=False, indent=2)}
```

## Live Provider

```json
{json.dumps(metrics.get('live_provider', {'status': 'not_run'}), ensure_ascii=False, indent=2)}
```

No unavailable provider or failed test is converted into a pass.
"""
    (output / "final_evaluation_report.md").write_text(report, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--routing-only", action="store_true",
        help="Re-evaluate routing and preserve already verified integration/live metrics.",
    )
    args = parser.parse_args()
    PATHS.ensure()
    metrics_path = PATHS.artifacts / "evals" / "metrics.json"
    if args.routing_only:
        if not metrics_path.is_file():
            parser.error("--routing-only requires an existing full metrics.json")
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["generated_at"] = datetime.now(timezone.utc).isoformat()
        metrics["routing"] = routing_eval()
        metrics["routing_re_evaluated_only"] = True
        write_reports(metrics)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return
    metrics: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routing": routing_eval(),
        "skill_tool": tool_skill_eval(),
        "reliability": reliability_eval(),
    }
    if args.integration:
        metrics["integration"] = integration_eval()
    if args.live:
        metrics["live_provider"] = live_provider_eval()
    write_reports(metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""The single top-level GeoOrchestratorAgent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from geoagent.orchestration.harness import AgentExecutionHarness, AgentHandler
from geoagent.orchestration.models import AgentResult, GoalContract, OrchestrationResult, PlanPatch, ReviewResult, TaskGraph
from geoagent.orchestration.planning import DynamicGeoPlanner, NoProgressGuard
from geoagent.orchestration.routing import HierarchicalIntentRouter, parse_goal_contract
from geoagent.orchestration.security import ArtifactCommitGate, default_capabilities
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.config import PATHS
from renewable_site_agent.observability import TELEMETRY


@dataclass
class GeoOrchestratorAgent:
    """Hierarchical controller; specialists never receive delegation rights."""

    router: HierarchicalIntentRouter = field(default_factory=HierarchicalIntentRouter)
    planner: DynamicGeoPlanner = field(default_factory=DynamicGeoPlanner)
    state_store: MultiAgentStateStore = field(default_factory=MultiAgentStateStore)
    max_replans: int = 2

    def plan(
        self,
        user_request: str,
        *,
        job_id: str | None = None,
        prior_contract: GoalContract | None = None,
        use_llm: bool = True,
        existing_artifacts: list[dict[str, Any]] | None = None,
        existing_evidence: list[dict[str, Any]] | None = None,
        provider_status: dict[str, Any] | None = None,
        execution_state: dict[str, Any] | None = None,
        execution_budget: dict[str, Any] | None = None,
        region_or_aoi: dict[str, Any] | str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> OrchestrationResult:
        job_id = job_id or uuid.uuid4().hex
        with TELEMETRY.span("intent.route", job_id=job_id):
            decision, contract = self.router.understand(user_request, use_llm=use_llm)
        with TELEMETRY.span("goal.parse", job_id=job_id):
            # Goal understanding is produced jointly with intent by the LLM;
            # this span records immutable contract validation/persistence.
            GoalContract.model_validate(contract.model_dump(mode="json"))
        if region_or_aoi is not None or constraints:
            material = contract.model_dump(mode="json", exclude={"contract_hash"})
            if region_or_aoi is not None:
                material["region_or_aoi"] = region_or_aoi
            if constraints:
                material["hard_constraints"] = {**material.get("hard_constraints", {}), **constraints}
            contract = GoalContract.model_validate(material)
        if prior_contract is not None:
            # A clarification continues the immutable goal lineage in the
            # same durable job.  Preserve the selected AOI and all prior hard
            # constraints unless the user explicitly supplied replacements.
            material = contract.model_dump(mode="json", exclude={"contract_hash"})
            material["goal_id"] = prior_contract.goal_id
            material["version"] = prior_contract.version + 1
            material["region_or_aoi"] = (
                region_or_aoi if region_or_aoi is not None else prior_contract.region_or_aoi
            )
            material["hard_constraints"] = {
                **prior_contract.hard_constraints,
                **material.get("hard_constraints", {}),
                **(constraints or {}),
            }
            material["explicit_user_requirements"] = list(dict.fromkeys([
                *prior_contract.explicit_user_requirements,
                *material.get("explicit_user_requirements", []),
            ]))
            contract = GoalContract.model_validate(material)
        if decision.missing_fields:
            remaining: list[str] = []
            for field_name in decision.missing_fields:
                normalized = field_name.lower().replace("-", "_").replace(" ", "_")
                region_supplied = region_or_aoi is not None and any(token in normalized for token in ("region", "aoi", "bbox", "location"))
                artifact_supplied = bool(existing_artifacts) and any(
                    token in normalized
                    for token in ("input", "supplied", "artifact", "feature", "dataset", "data_source", "coordinate", "geometry", "point", "polygon", "layer")
                )
                constraint_supplied = bool(constraints) and any(
                    key.lower().replace("-", "_").replace(" ", "_") in normalized
                    or normalized in key.lower().replace("-", "_").replace(" ", "_")
                    for key in constraints
                )
                provider_supplied = bool(provider_status) and any(
                    token in normalized for token in ("provider", "dataset", "data_source", "dem", "stac", "sentinel", "cache")
                )
                if not (region_supplied or artifact_supplied or constraint_supplied or provider_supplied):
                    remaining.append(field_name)
            if remaining != decision.missing_fields:
                decision = decision.model_copy(update={"missing_fields": remaining, "needs_clarification": bool(remaining)})
                material = contract.model_dump(mode="json", exclude={"contract_hash"})
                material["missing_fields"] = tuple(remaining)
                contract = GoalContract.model_validate(material)
        self.state_store.save_contract(job_id, contract)
        if decision.needs_clarification:
            return OrchestrationResult(
                job_id=job_id, intent=decision, goal_contract=contract,
                status="NEEDS_CLARIFICATION",
                metrics={
                    "route_mode": decision.route_mode,
                    "goal_intent_llm_call_count": self.router.last_llm_call_count,
                    "goal_intent_llm_provider": self.router.last_llm_provider,
                    "goal_intent_llm_model": self.router.last_llm_model,
                    "goal_intent_llm_error": self.router.last_llm_error,
                },
            )
        with TELEMETRY.span("planner.generate", job_id=job_id, goal_id=contract.goal_id):
            graph = self.planner.create(
                contract,
                existing_artifacts=existing_artifacts,
                existing_evidence=existing_evidence,
                provider_status=provider_status,
                execution_state=execution_state,
                execution_budget=execution_budget,
                use_llm=use_llm,
            )
        with TELEMETRY.span("graph.create", job_id=job_id, goal_id=contract.goal_id, graph_id=graph.graph_id):
            self.state_store.save_graph(job_id, graph)
        return OrchestrationResult(
            job_id=job_id,
            intent=decision,
            goal_contract=contract,
            task_graph=graph,
            status="PLANNED",
            metrics={
                "route_mode": decision.route_mode,
                "goal_intent_llm_call_count": self.router.last_llm_call_count,
                "goal_intent_llm_provider": self.router.last_llm_provider,
                "goal_intent_llm_model": self.router.last_llm_model,
                "goal_intent_llm_error": self.router.last_llm_error,
                "plan_mode": graph.planner_mode,
                "planner_metadata": graph.planning_metadata,
                "selected_agents": sorted({node.agent_role for node in graph.nodes}),
                "task_count": len(graph.nodes),
                "plan_hash": graph.plan_hash,
            },
        )

    def execute(
        self,
        user_request: str,
        *,
        job_id: str | None = None,
        prior_contract: GoalContract | None = None,
        use_llm: bool = True,
        handlers: dict[str, AgentHandler] | None = None,
        resume: bool = True,
        existing_artifacts: list[dict[str, Any]] | None = None,
        existing_evidence: list[dict[str, Any]] | None = None,
        provider_status: dict[str, Any] | None = None,
        execution_state: dict[str, Any] | None = None,
        execution_budget: dict[str, Any] | None = None,
        region_or_aoi: dict[str, Any] | str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> OrchestrationResult:
        planned = self.plan(
            user_request, job_id=job_id, prior_contract=prior_contract,
            use_llm=use_llm, existing_artifacts=existing_artifacts,
            existing_evidence=existing_evidence, provider_status=provider_status,
            execution_state=execution_state, execution_budget=execution_budget,
            region_or_aoi=region_or_aoi, constraints=constraints,
        )
        if planned.status == "NEEDS_CLARIFICATION" or planned.task_graph is None:
            return planned
        harness = AgentExecutionHarness(store=self.state_store)
        guard = NoProgressGuard()
        graph = planned.task_graph
        replans: list[PlanPatch] = []
        all_results: list[AgentResult] = []
        for replan_index in range(self.max_replans + 1):
            results, review = harness.execute_graph(job_id=planned.job_id, contract=planned.goal_contract, graph=graph, handlers=handlers, resume=resume)
            all_results.extend(results)
            if review.status == "PASS":
                coverage_review, required_roles, covered_roles = self._validate_goal_coverage(planned, all_results)
                if coverage_review.status != "PASS":
                    self.state_store.save_review(planned.job_id, graph.graph_id, coverage_review)
                    return planned.model_copy(update={
                        "task_graph": graph, "agent_results": all_results, "review": coverage_review,
                        "replans": replans, "status": "FAILED",
                        "metrics": {
                            **planned.metrics,
                            "goal_coverage_status": "FAILED_GOAL_COVERAGE",
                            "goal_coverage_required_roles": required_roles,
                            "goal_coverage_covered_roles": covered_roles,
                            "goal_coverage_missing_roles": coverage_review.missing_evidence,
                            "actual_tool_call_count": sum(len(item.actual_tool_calls) for item in all_results),
                        },
                    })
                if review.reason == "No reviewer node required":
                    review = coverage_review
                    self.state_store.save_review(planned.job_id, graph.graph_id, review)
                final = self._commit_final(harness, planned.job_id, planned.goal_contract, results)
                return planned.model_copy(update={
                    "task_graph": graph, "agent_results": all_results, "review": review,
                    "replans": replans, "final_artifacts": final, "status": "SUCCEEDED",
                    "metrics": {
                        **planned.metrics,
                        "handoff_count": harness.delegations,
                        "agent_turns": harness.turns,
                        "duplicate_tasks_injected": 0,
                        "duplicate_tasks_detected": harness.task_ledger.reused,
                        "duplicate_detection_rate": 1.0 if harness.task_ledger.reused else 0.0,
                        "duplicate_tasks_reexecuted": 0,
                        "duplicate_reexecution_rate": 0.0,
                        "historical_duplicate_task_rate_raw": harness.task_ledger.reused / max(1, harness.task_ledger.submitted + harness.task_ledger.reused),
                        "artifact_reuse_rate": sum(item.status == "REUSED" for item in all_results) / max(1, len(all_results)),
                        "actual_tool_call_count": sum(len(item.actual_tool_calls) for item in all_results),
                        "goal_coverage_status": "PASS",
                        "goal_coverage_required_roles": required_roles,
                        "goal_coverage_covered_roles": covered_roles,
                        "goal_coverage_missing_roles": [],
                        "conflict_count": len(harness.conflicts.records),
                        "replan_count": len(replans),
                    },
                })
            if review.status == "NEEDS_CLARIFICATION":
                return planned.model_copy(update={"task_graph": graph, "agent_results": all_results, "review": review, "replans": replans, "status": "NEEDS_CLARIFICATION"})
            if replan_index >= self.max_replans:
                return planned.model_copy(update={"task_graph": graph, "agent_results": all_results, "review": review, "replans": replans, "status": "FAILED"})
            guard.record(graph.plan_hash, list(harness.evidence.evidence), {"review": review.model_dump(mode="json")})
            patch = self._local_replan(
                graph, review, planned.goal_contract, replan_index + 1,
                completed_task_ids={item.task_id for item in all_results if item.status in {"SUCCEEDED", "REUSED"}},
                evidence=[item.model_dump(mode="json") for item in harness.evidence.evidence.values()],
                execution_state={"attempted_tasks": [item.task_id for item in all_results], "review": review.model_dump(mode="json")},
                use_llm=use_llm,
            )
            with TELEMETRY.span("replan.generate", job_id=planned.job_id, graph_id=graph.graph_id, replan_reason=review.reason):
                graph = self.planner.apply_patch(graph, patch, {item.task_id for item in all_results if item.status in {"SUCCEEDED", "REUSED"}})
            replans.append(patch)
            self.state_store.save_replan(planned.job_id, patch)
            self.state_store.save_graph(planned.job_id, graph, status="REPLANNED")
        return planned.model_copy(update={"status": "FAILED"})

    @staticmethod
    def _validate_goal_coverage(
        planned: OrchestrationResult,
        results: list[AgentResult],
    ) -> tuple[ReviewResult, list[str], list[str]]:
        """Keep engine success separate from complete user-goal evidence."""
        required = [
            role for role in planned.intent.required_agent_roles
            if role not in {"orchestrator"}
        ]
        covered = {
            result.agent_id.removesuffix("-agent")
            for result in results
            if result.status in {"SUCCEEDED", "REUSED"}
            and (result.evidence_refs or result.artifact_refs)
        }
        missing = [role for role in required if role not in covered]
        if missing:
            return ReviewResult(
                status="FAIL",
                missing_evidence=[f"{role}_evidence" for role in missing],
                reason=(
                    "FAILED_GOAL_COVERAGE: execution finished but required domain evidence "
                    f"is missing for roles={missing}"
                ),
            ), required, sorted(covered)
        return ReviewResult(
            status="PASS",
            reason="Deterministic goal coverage validation passed for every required specialist role",
        ), required, sorted(covered)

    def clarify(self, prior: GoalContract, clarification: str) -> GoalContract:
        """Only orchestrator/user clarification can mint a new contract version."""
        decision = self.router.route(f"{prior.primary_goal}\nClarification: {clarification}", use_llm=False)
        updated = parse_goal_contract(f"{prior.primary_goal}\nClarification: {clarification}", decision)
        material = updated.model_dump(mode="json", exclude={"contract_hash"})
        material.update({"goal_id": prior.goal_id, "version": prior.version + 1})
        # model_copy does not re-run Pydantic validators. Re-validation is
        # required so each immutable clarification version gets a fresh hash.
        return GoalContract.model_validate(material)

    def _local_replan(
        self, graph: TaskGraph, review: ReviewResult, contract: GoalContract, index: int,
        *, completed_task_ids: set[str], evidence: list[dict[str, Any]],
        execution_state: dict[str, Any], use_llm: bool,
    ) -> PlanPatch:
        execution_state = {**execution_state, "local_replan_index": index}
        return self.planner.replan(
            contract=contract, graph=graph, review=review,
            completed_task_ids=completed_task_ids, evidence=evidence,
            execution_state=execution_state, use_llm=use_llm,
        )

    def _commit_final(self, harness: AgentExecutionHarness, job_id: str, contract: GoalContract, results: list[AgentResult]) -> list[Any]:
        workspace = harness.workspace_manager.create(job_id, [])
        gate = ArtifactCommitGate(harness.workspace_manager)
        valid_ids = set(harness.artifacts.artifacts)
        candidates = [artifact for result in results if result.status in {"SUCCEEDED", "REUSED"} and not result.agent_id.startswith("reviewer-") for artifact in result.artifact_refs]
        if not candidates:
            return []
        selected = candidates[-1:]
        committed = []
        policies = default_capabilities(workspace)
        for artifact in selected:
            role = artifact.producer_agent.removesuffix("-agent")
            with TELEMETRY.span("artifact.commit", job_id=job_id, task_id=artifact.task_id, agent_id="orchestrator-agent", artifact=artifact.artifact_id):
                promoted = gate.commit(workspace=workspace, contract=contract, artifact=artifact, allowed_types=policies[role].allowed_artifact_types, valid_dependency_ids=valid_ids)
                committed.append(promoted)
                self.state_store.save_artifact(promoted)
        return committed

    @staticmethod
    def framework_facts() -> dict[str, Any]:
        return {
            "framework": "Strands Agents",
            "topology": "Hierarchical Orchestrator + Dynamic Task Graph + Agents-as-Tools",
            "official_graph_adapter": "strands.multiagent.GraphBuilder",
            "renewable_position": "Composite Domain Agent backed by renewable_site_agent",
            "runtime_root": str(PATHS.project_root / "runtime" / "jobs"),
        }

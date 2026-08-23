"""Governed specialist execution, DAG scheduling, dedup, review and conflict handling."""

from __future__ import annotations

import json
import inspect
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from geoagent.orchestration.models import (
    AgentCapabilityPolicy,
    AgentResult,
    ArtifactRef,
    ConflictRecord,
    EvidenceRef,
    GoalContract,
    ReviewResult,
    TaskGraph,
    TaskNode,
    TaskStatus,
    stable_hash,
)
from geoagent.orchestration.security import ArtifactCommitGate, JobWorkspace, WorkspaceManager, default_capabilities, sha256_file
from geoagent.orchestration.state import MultiAgentStateStore
from renewable_site_agent.observability import TELEMETRY
from renewable_site_agent.reliability.executor import ExecutionBudget, ReliableToolExecutor
from renewable_site_agent.schemas import ToolResult


ToolInvoker = Callable[[str, Callable[[], Any], str | None, str], Any]
AgentHandler = Callable[..., dict[str, Any]]


SYSTEM_PROMPTS = {
    "data": "Acquire only required AOI data; preserve license, checksum and provider provenance.",
    "gis_analysis": "Perform bounded deterministic vector/raster operations. Never change the goal.",
    "terrain": "Produce metric-CRS terrain evidence and explicit nodata handling.",
    "remote_sensing": "Search current STAC and read only required bands/windows; never fetch full scenes by default.",
    "renewable_siting": "Use shared evidence for domain scoring; do not duplicate data or GIS acquisition.",
    "map_report": "Create concise maps/reports from validated artifacts; never alter analytical evidence.",
    "reviewer": "Read-only review. Return PASS, REPLAN, NEEDS_CLARIFICATION or FAIL; never rerun tools.",
}


@dataclass
class TaskLedger:
    by_signature: dict[str, AgentResult] = field(default_factory=dict)
    submitted: int = 0
    reused: int = 0

    def lookup(self, signature: str) -> AgentResult | None:
        value = self.by_signature.get(signature)
        if value and all(Path(item.path).is_file() and sha256_file(Path(item.path)) == item.checksum_sha256 for item in value.artifact_refs):
            self.reused += 1
            return value.model_copy(update={"status": "REUSED"})
        return None

    def put(self, signature: str, result: AgentResult) -> None:
        self.submitted += 1
        if result.status == "SUCCEEDED":
            self.by_signature[signature] = result


@dataclass
class ArtifactRegistry:
    artifacts: dict[str, ArtifactRef] = field(default_factory=dict)

    def add(self, artifact: ArtifactRef) -> None:
        prior = self.artifacts.get(artifact.artifact_id)
        if prior and prior.checksum_sha256 != artifact.checksum_sha256:
            raise ValueError("DUPLICATE_ARTIFACT_OWNERSHIP")
        self.artifacts[artifact.artifact_id] = artifact

    def for_tasks(self, task_ids: list[str]) -> list[ArtifactRef]:
        return [item for item in self.artifacts.values() if item.task_id in task_ids]


@dataclass
class EvidenceRegistry:
    evidence: dict[str, EvidenceRef] = field(default_factory=dict)

    def add_many(self, values: list[EvidenceRef]) -> None:
        for value in values:
            self.evidence[value.evidence_id] = value


@dataclass
class ConflictResolver:
    records: list[ConflictRecord] = field(default_factory=list)

    def resolve(self, *, contract: GoalContract, proposals: list[dict[str, Any]]) -> ConflictRecord | None:
        hard = contract.hard_constraints
        violations = [item for item in proposals if any(key in item and item[key] != value for key, value in hard.items())]
        if violations:
            record = ConflictRecord(kind="goal_constraint", parties=[str(item.get("agent_id", "unknown")) for item in violations], resolution="REVIEW", reason="Agent proposal conflicts with immutable hard constraint")
            self.records.append(record)
            return record
        crs_values = {item.get("crs") for item in proposals if item.get("crs")}
        if len(crs_values) > 1:
            record = ConflictRecord(kind="crs_mismatch", parties=[str(item.get("agent_id", "unknown")) for item in proposals], resolution="DETERMINISTIC", reason=f"Normalize CRS before comparison: {sorted(crs_values)}")
            self.records.append(record)
            return record
        evidence_by_key: dict[str, set[str]] = {}
        for proposal in proposals:
            for key, value in proposal.get("claims", {}).items():
                evidence_by_key.setdefault(key, set()).add(json.dumps(value, sort_keys=True))
        if any(len(values) > 1 for values in evidence_by_key.values()):
            record = ConflictRecord(kind="contradictory_evidence", parties=[str(item.get("agent_id", "unknown")) for item in proposals], resolution="REVIEW", reason="Preserve both provenance records for reviewer")
            self.records.append(record)
            return record
        return None


@dataclass
class SpecialistAgent:
    role: str
    agent_id: str
    policy: AgentCapabilityPolicy
    workspace: JobWorkspace
    workspace_manager: WorkspaceManager
    handler: AgentHandler | None = None
    system_prompt: str = ""
    tool_calls: int = 0

    def __post_init__(self) -> None:
        if not self.system_prompt:
            self.system_prompt = SYSTEM_PROMPTS[self.role]

    def execute(self, task: TaskNode, contract: GoalContract, inputs: list[ArtifactRef]) -> AgentResult:
        started = time.perf_counter()
        if task.agent_role != self.role:
            raise PermissionError(f"Agent role mismatch: {self.role} cannot execute {task.agent_role}")
        if self.role == "reviewer":
            return self._review_task(task, contract, inputs)
        actual_calls: list[dict[str, Any]] = []

        def invoke_tool(tool_name: str, operation: Callable[[], Any], provider: str | None = None, execution_class: str = "OFFLINE_LOCAL_DATA_EXECUTED") -> Any:
            if tool_name not in task.allowed_tools:
                raise PermissionError(f"TASK_TOOL_NOT_PLANNED: {tool_name}")
            self.workspace_manager.authorize_tool(
                workspace=self.workspace, policy=self.policy, agent_id=self.agent_id,
                tool_name=tool_name, provider=provider,
            )
            self.tool_calls += 1
            if self.tool_calls > self.policy.max_tool_calls:
                raise RuntimeError("AGENT_TOOL_BUDGET_EXCEEDED")
            started_call = time.perf_counter()
            record = {
                "tool": tool_name,
                "provider": provider,
                "execution_class": execution_class,
                "status": "RUNNING",
            }
            actual_calls.append(record)
            try:
                value = operation()
                if isinstance(value, dict) and value.get("_execution_class"):
                    record["execution_class"] = value.pop("_execution_class")
                record["status"] = "SUCCEEDED"
                return value
            except Exception as exc:
                record.update({"status": "FAILED", "error_type": type(exc).__name__})
                raise
            finally:
                record["latency_ms"] = round((time.perf_counter() - started_call) * 1000, 3)

        if self.handler:
            parameter_count = len(inspect.signature(self.handler).parameters)
            payload = self.handler(task, contract, inputs, invoke_tool) if parameter_count >= 4 else self.handler(task, contract, inputs)
        else:
            payload = {
            "objective": task.objective,
            "contract_hash": contract.contract_hash,
            "input_artifacts": [item.artifact_id for item in inputs],
            "planned_allowed_tools": task.allowed_tools,
            "execution_class": "SKIPPED",
            "skip_reason": "No formal runtime handler was supplied",
            "execution_mode": "governed_specialist",
            }
        artifact_type = task.expected_outputs[0].removesuffix("_artifact") if task.expected_outputs else f"{self.role}_result"
        if artifact_type not in self.policy.allowed_artifact_types:
            artifact_type = self.policy.allowed_artifact_types[0]
        path = self.workspace.agent_staging(self.agent_id) / f"{task.task_id}.json"
        destination = self.workspace_manager.write_bytes(
            workspace=self.workspace,
            policy=self.policy,
            agent_id=self.agent_id,
            path=path,
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, indent=2).encode("utf-8"),
        )
        artifact = ArtifactRef(
            job_id=self.workspace.job_id,
            task_id=task.task_id,
            producer_agent=self.agent_id,
            artifact_type=artifact_type,
            path=str(destination),
            checksum_sha256=sha256_file(destination),
            provenance=[item.artifact_id for item in inputs],
            dependency_artifact_ids=[item.artifact_id for item in inputs],
            contract_hash=contract.contract_hash,
        )
        evidence = EvidenceRef(source=self.agent_id, summary=f"{self.role} completed {task.objective}", provenance={"task_signature": task.task_signature, "artifact": artifact.artifact_id})
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            status="SUCCEEDED",
            summary=f"{self.role} completed governed task",
            artifact_refs=[artifact],
            evidence_refs=[evidence],
            metrics={"latency_ms": (time.perf_counter() - started) * 1000, "tool_calls": len(actual_calls), "context_artifact_count": len(inputs)},
            actual_tool_calls=actual_calls,
        )

    def _review_task(self, task: TaskNode, contract: GoalContract, inputs: list[ArtifactRef]) -> AgentResult:
        missing = [item for item in task.required_inputs if not any(ref.artifact_type == item or ref.task_id in item for ref in inputs)]
        invalid = [ref.artifact_id for ref in inputs if not Path(ref.path).is_file() or sha256_file(Path(ref.path)) != ref.checksum_sha256]
        status = "FAILED" if invalid else ("BLOCKED" if missing else "SUCCEEDED")
        review = ReviewResult(
            status="FAIL" if invalid else ("REPLAN" if missing else "PASS"),
            missing_evidence=missing,
            provenance_issues=invalid,
            recommended_replan_nodes=task.dependencies if missing else [],
            reason="Deterministic read-only review",
        )
        path = self.workspace.agent_staging(self.agent_id) / f"{task.task_id}-review.json"
        destination = self.workspace_manager.write_bytes(workspace=self.workspace, policy=self.policy, agent_id=self.agent_id, path=path, data=review.model_dump_json(indent=2).encode("utf-8"))
        artifact = ArtifactRef(job_id=self.workspace.job_id, task_id=task.task_id, producer_agent=self.agent_id, artifact_type="review_result", path=str(destination), checksum_sha256=sha256_file(destination), provenance=[item.artifact_id for item in inputs], dependency_artifact_ids=[item.artifact_id for item in inputs], contract_hash=contract.contract_hash)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=status, summary=review.reason, artifact_refs=[artifact], metrics={"review_status": review.status, "tool_calls": 0}, actual_tool_calls=[])

    def as_strands_tool(self) -> Any:
        """Expose this specialist through the official Strands tool decorator."""
        from strands import tool

        specialist = self

        @tool(name=f"delegate_{self.role}_agent", description=self.system_prompt)
        def delegate(task_json: str, contract_json: str, input_artifacts_json: str = "[]") -> str:
            task = TaskNode.model_validate_json(task_json)
            contract = GoalContract.model_validate_json(contract_json)
            refs = [ArtifactRef.model_validate(item) for item in json.loads(input_artifacts_json)]
            return specialist.execute(task, contract, refs).model_dump_json()

        return delegate


@dataclass
class AgentExecutionHarness:
    store: MultiAgentStateStore
    workspace_manager: WorkspaceManager = field(default_factory=WorkspaceManager)
    task_ledger: TaskLedger = field(default_factory=TaskLedger)
    artifacts: ArtifactRegistry = field(default_factory=ArtifactRegistry)
    evidence: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    conflicts: ConflictResolver = field(default_factory=ConflictResolver)
    max_parallel_agents: int = 4
    max_agent_delegations: int = 32
    max_agent_turns: int = 48
    delegations: int = 0
    turns: int = 0

    def execute_graph(
        self,
        *,
        job_id: str,
        contract: GoalContract,
        graph: TaskGraph,
        handlers: dict[str, AgentHandler] | None = None,
        resume: bool = True,
        crash_after_completed: int | None = None,
    ) -> tuple[list[AgentResult], ReviewResult]:
        roles = sorted({node.agent_role for node in graph.nodes})
        agent_ids = [f"{role}-agent" for role in roles] + ["orchestrator-agent"]
        self.workspace_manager.audit_hook = self.store.record_policy
        workspace = self.workspace_manager.create(job_id, agent_ids)
        policies = default_capabilities(workspace)
        agents = {
            role: SpecialistAgent(role=role, agent_id=f"{role}-agent", policy=policies[role], workspace=workspace, workspace_manager=self.workspace_manager, handler=(handlers or {}).get(role))
            for role in roles
        }
        orchestrator_policy = policies["orchestrator"]
        self.store.save_contract(job_id, contract)
        self.store.save_graph(job_id, graph)
        self._active_graph_id = graph.graph_id
        completed: set[str] = set()
        results: list[AgentResult] = []
        review = ReviewResult(status="PASS", reason="No reviewer node required")
        while len(completed) < len(graph.nodes):
            ready = [node for node in graph.nodes if node.task_id not in completed and set(node.dependencies).issubset(completed)]
            if not ready:
                review = ReviewResult(status="FAIL", reason="No executable nodes; graph stalled")
                break
            batch_results: dict[str, AgentResult] = {}
            with ThreadPoolExecutor(max_workers=min(self.max_parallel_agents, len(ready)), thread_name_prefix="geo-specialist") as pool:
                futures = {}
                for node in ready:
                    reused = self.task_ledger.lookup(node.task_signature)
                    if reused is None and resume:
                        with TELEMETRY.span("worker.resume", job_id=job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_role=node.agent_role):
                            reused = self.store.reusable_result(node.task_signature)
                    if reused is not None:
                        batch_results[node.task_id] = reused.model_copy(update={"task_id": node.task_id})
                        continue
                    with TELEMETRY.span("agent.delegate", job_id=job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_id="orchestrator-agent", agent_role=node.agent_role):
                        self.workspace_manager.authorize_delegate(workspace=workspace, caller_id="orchestrator-agent", caller_policy=orchestrator_policy, target_role=node.agent_role)
                    self.delegations += 1
                    self.turns += 1
                    if self.delegations > self.max_agent_delegations or self.turns > self.max_agent_turns:
                        raise RuntimeError("MULTI_AGENT_BUDGET_EXCEEDED")
                    inputs = self.artifacts.for_tasks(node.dependencies)
                    futures[pool.submit(self._execute_one, agents[node.agent_role], node, contract, inputs)] = node
                for future in as_completed(futures):
                    node = futures[future]
                    batch_results[node.task_id] = future.result()
            for node in ready:
                result = batch_results[node.task_id]
                results.append(result)
                self.task_ledger.put(node.task_signature, result)
                self.evidence.add_many(result.evidence_refs)
                for artifact in result.artifact_refs:
                    self.artifacts.add(artifact)
                    self.store.save_artifact(artifact)
                completed.add(node.task_id)
                status = "REUSED" if result.status == "REUSED" else ("SUCCEEDED" if result.status == "SUCCEEDED" else "FAILED")
                with TELEMETRY.span("checkpoint.write", job_id=job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_id=result.agent_id, artifact=",".join(item.artifact_id for item in result.artifact_refs)):
                    self.store.update_node(graph.graph_id, node.task_id, status, [item.artifact_id for item in result.artifact_refs])
                if node.agent_role == "reviewer" and result.artifact_refs:
                    with TELEMETRY.span("review.execute", job_id=job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_id=result.agent_id):
                        review = ReviewResult.model_validate_json(Path(result.artifact_refs[0].path).read_text(encoding="utf-8"))
                        self.store.save_review(job_id, graph.graph_id, review)
                if result.status not in {"SUCCEEDED", "REUSED"}:
                    return results, ReviewResult(status="REPLAN", recommended_replan_nodes=[node.task_id], reason=result.summary)
            # Fault-injection hook used by the recovery test/eval. It fires
            # only after the complete ready batch has been durably recorded,
            # accurately modelling a worker crash between graph layers.
            if crash_after_completed is not None and len(completed) >= crash_after_completed:
                raise RuntimeError("INJECTED_WORKER_CRASH")
        return results, review

    def _execute_one(self, agent: SpecialistAgent, node: TaskNode, contract: GoalContract, inputs: list[ArtifactRef]) -> AgentResult:
        started = time.perf_counter()
        reliable = ReliableToolExecutor(budget=ExecutionBudget(max_steps=2, max_tool_calls=max(1, agent.policy.max_tool_calls), max_llm_calls=0, max_total_runtime_seconds=agent.policy.max_runtime_seconds, max_retries_per_step=2))
        with TELEMETRY.span("agent.execute", job_id=agent.workspace.job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_id=agent.agent_id, agent_role=agent.role, workspace=str(agent.workspace.scratch(agent.agent_id))):
            with TELEMETRY.span("tool.execute", job_id=agent.workspace.job_id, goal_id=contract.goal_id, task_id=node.task_id, agent_id=agent.agent_id, agent_role=agent.role, tool=f"agent:{agent.role}", provider=f"agent:{agent.role}"):
                wrapped = reliable.execute(tool_name=f"agent:{agent.role}", arguments={}, operation=lambda: ToolResult.ok(artifact=agent.execute(node, contract, inputs).model_dump(mode="json")), provider=f"agent:{agent.role}", timeout_seconds=agent.policy.max_runtime_seconds, max_attempts=2)
        if wrapped.status == "failed" or not isinstance(wrapped.artifact, dict):
            result = AgentResult(task_id=node.task_id, agent_id=agent.agent_id, status="FAILED", summary=wrapped.message, metrics={"error_code": wrapped.error_code.value if wrapped.error_code else None})
        else:
            result = AgentResult.model_validate(wrapped.artifact)
        self.store.save_attempt(agent.workspace.job_id, node.goal_id if False else self._graph_id_for(node), result, int(wrapped.metrics.get("attempt", 1)), (time.perf_counter() - started) * 1000)
        return result

    def _graph_id_for(self, node: TaskNode) -> str:
        # Filled by execute_graph before calls through a transient attribute to
        # keep the specialist handoff contract graph-agnostic.
        return getattr(self, "_active_graph_id", node.goal_id)

    def to_strands_graph(self, graph: TaskGraph, executors: dict[str, Any]) -> Any:
        """Build the equivalent official Strands Graph for LLM-backed execution."""
        from strands.multiagent import GraphBuilder

        builder = GraphBuilder()
        for node in graph.nodes:
            if node.task_id not in executors:
                raise KeyError(f"Missing Strands executor for {node.task_id}")
            builder.add_node(executors[node.task_id], node_id=node.task_id)
        for node in graph.nodes:
            for dependency in node.dependencies:
                builder.add_edge(dependency, node.task_id)
        builder.set_max_node_executions(1)
        return builder.build()

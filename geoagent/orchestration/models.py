"""Immutable contracts for hierarchical dynamic GeoAgent orchestration."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


GeoIntent = Literal[
    "general_gis",
    "data_discovery",
    "terrain_analysis",
    "remote_sensing",
    "renewable_siting",
    "map_visualization",
    "multi_domain",
    "unknown",
]

AgentRole = Literal[
    "orchestrator",
    "data",
    "gis_analysis",
    "terrain",
    "remote_sensing",
    "renewable_siting",
    "map_report",
    "reviewer",
]


class GeoIntentDecision(BaseModel):
    intent: GeoIntent
    subtype: Literal["solar", "wind", "storage", "hybrid", "unknown"] = "unknown"
    confidence: float = Field(default=0.75, ge=0, le=1)
    required_agent_roles: list[AgentRole] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    route_mode: Literal["legacy_keyword", "deterministic", "llm"] = "llm"
    reason: str = ""

    @field_validator("required_agent_roles", "missing_fields", mode="before")
    @classmethod
    def normalize_provider_list_fields(cls, value: Any) -> Any:
        """Accept JSON-string or delimiter-separated arrays from compatible APIs."""
        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                return parsed
            return [item.strip(" `\"'") for item in re.split(r"[,;，；\n]", stripped) if item.strip(" `\"'")]
        return value


class GoalContract(BaseModel):
    """Job-time immutable statement of user intent and precedence."""

    model_config = ConfigDict(frozen=True)

    goal_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    version: int = Field(default=1, ge=1)
    primary_goal: str = Field(min_length=1)
    intent: GeoIntent
    subtype: str = "unknown"
    region_or_aoi: dict[str, Any] | str | None = None
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    explicit_user_requirements: list[str] = Field(default_factory=list)
    missing_fields: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    allowed_degradation: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    contract_hash: str = ""

    @model_validator(mode="after")
    def populate_hash(self) -> "GoalContract":
        if not self.contract_hash:
            material = self.model_dump(mode="json", exclude={"contract_hash"})
            object.__setattr__(self, "contract_hash", stable_hash(material))
        return self


class GeoGoalUnderstanding(BaseModel):
    """LLM-produced decision payload converted into an immutable contract."""

    decision: GeoIntentDecision
    primary_goal: str = Field(min_length=1)
    region_or_aoi: dict[str, Any] | str | None = None
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    explicit_user_requirements: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    allowed_degradation: list[str] = Field(default_factory=list)

    @field_validator("explicit_user_requirements", "expected_outputs", "allowed_degradation", mode="before")
    @classmethod
    def normalize_provider_list_fields(cls, value: Any) -> Any:
        return GeoIntentDecision.normalize_provider_list_fields(value)

    @model_validator(mode="before")
    @classmethod
    def accept_flat_decision_payload(cls, value: Any) -> Any:
        """Accept providers that flatten the nested intent decision.

        Some OpenAI-compatible providers occasionally emit the fields of the
        requested ``decision`` object at the top level.  This normalization is
        schema-preserving: it only moves known decision fields and still lets
        Pydantic validate every value against ``GeoIntentDecision``.
        """
        if not isinstance(value, dict):
            return value
        decision_fields = {
            "intent",
            "subtype",
            "confidence",
            "required_agent_roles",
            "missing_fields",
            "needs_clarification",
            "route_mode",
            "reason",
        }
        existing = value.get("decision")
        if "intent" not in value and not (isinstance(existing, dict) and existing.get("intent")):
            return value
        normalized = dict(value)
        decision = dict(existing) if isinstance(existing, dict) else {}
        decision.update({name: normalized.pop(name) for name in decision_fields if name in normalized})
        normalized["decision"] = decision
        return normalized

    def to_contract(self, original_query: str) -> GoalContract:
        requirements = self.explicit_user_requirements or [original_query]
        return GoalContract(
            primary_goal=self.primary_goal,
            intent=self.decision.intent,
            subtype=self.decision.subtype,
            region_or_aoi=self.region_or_aoi,
            hard_constraints=self.hard_constraints,
            soft_preferences=self.soft_preferences,
            explicit_user_requirements=requirements,
            missing_fields=tuple(self.decision.missing_fields),
            expected_outputs=tuple(self.expected_outputs or ["structured_result"]),
            allowed_degradation=tuple(self.allowed_degradation or ["provider_fallback_with_provenance"]),
        )


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REUSED = "REUSED"
    CANCELLED = "CANCELLED"


class TaskNode(BaseModel):
    task_id: str
    goal_id: str
    agent_role: AgentRole
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    cost_hint: Literal["low", "medium", "high"] = "low"
    status: TaskStatus = TaskStatus.PENDING
    task_signature: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dependencies")
    @classmethod
    def no_self_dependency(cls, value: list[str], info: Any) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Task dependencies must be unique")
        return value

    def with_signature(self, contract: GoalContract) -> "TaskNode":
        normalized_objective = re.sub(r"\s+", " ", self.objective.strip().lower())
        signature = stable_hash(
            {
                "goal_id": contract.goal_id,
                "agent_role": self.agent_role,
                "objective": normalized_objective,
                "inputs": sorted(self.required_inputs),
                "outputs": sorted(self.expected_outputs),
            }
        )
        return self.model_copy(update={"task_signature": signature})


class LLMTaskNodeDraft(BaseModel):
    """Task node fields an LLM may propose before deterministic governance."""

    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    agent_role: AgentRole
    objective: str = Field(min_length=3, max_length=1000)
    dependencies: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    cost_hint: Literal["low", "medium", "high"] = "low"

    @field_validator("dependencies", "required_inputs", "expected_outputs", "allowed_tools", "success_criteria", mode="before")
    @classmethod
    def normalize_provider_list_fields(cls, value: Any) -> Any:
        return GeoIntentDecision.normalize_provider_list_fields(value)


class LLMTaskGraphDraft(BaseModel):
    """Schema-only LLM plan; converted to a validated governed TaskGraph."""

    nodes: list[LLMTaskNodeDraft] = Field(min_length=1)
    rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def discard_non_node_array_items(cls, value: Any) -> Any:
        """Ignore provider commentary accidentally inserted into ``nodes``.

        Only mapping-shaped node candidates survive this compatibility step;
        every surviving field is still validated by ``LLMTaskNodeDraft`` and
        the planner's role, tool, DAG, and budget gates remain authoritative.
        """
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
            return value
        normalized = dict(value)
        candidates: list[dict[str, Any]] = []
        for item in value["nodes"]:
            if isinstance(item, dict):
                candidates.append(item)
                continue
            if isinstance(item, LLMTaskNodeDraft):
                candidates.append(item.model_dump(mode="python"))
                continue
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    candidates.append(parsed)
        normalized["nodes"] = candidates
        return normalized


class TaskGraph(BaseModel):
    graph_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    goal_id: str
    nodes: list[TaskNode]
    created_at: datetime = Field(default_factory=utc_now)
    plan_hash: str = ""
    planner_mode: Literal["deterministic", "llm"] = "deterministic"
    planning_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> "TaskGraph":
        if not self.nodes:
            raise ValueError("TaskGraph requires at least one node")
        ids = [node.task_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Task ids must be unique")
        known = set(ids)
        for node in self.nodes:
            unknown = set(node.dependencies) - known
            if unknown:
                raise ValueError(f"Unknown dependencies for {node.task_id}: {sorted(unknown)}")
            if node.task_id in node.dependencies:
                raise ValueError(f"Self-cycle in {node.task_id}")
        indegree = {node_id: 0 for node_id in ids}
        children: dict[str, list[str]] = {node_id: [] for node_id in ids}
        for node in self.nodes:
            indegree[node.task_id] = len(node.dependencies)
            for parent in node.dependencies:
                children[parent].append(node.task_id)
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if visited != len(ids):
            raise ValueError("TaskGraph contains a cycle")
        if not self.plan_hash:
            material = [node.model_dump(mode="json", exclude={"status"}) for node in self.nodes]
            object.__setattr__(self, "plan_hash", stable_hash(material))
        return self

    def node(self, task_id: str) -> TaskNode:
        return next(node for node in self.nodes if node.task_id == task_id)

    def ready(self, completed: set[str]) -> list[TaskNode]:
        return [
            node for node in self.nodes
            if node.status in {TaskStatus.PENDING, TaskStatus.READY}
            and set(node.dependencies).issubset(completed)
        ]


class ArtifactRef(BaseModel):
    artifact_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str
    task_id: str
    producer_agent: str
    artifact_type: str
    path: str
    checksum_sha256: str
    provenance: list[str] = Field(default_factory=list)
    dependency_artifact_ids: list[str] = Field(default_factory=list)
    contract_hash: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EvidenceRef(BaseModel):
    evidence_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: str
    summary: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    task_id: str
    agent_id: str
    status: Literal["SUCCEEDED", "FAILED", "BLOCKED", "NEEDS_CLARIFICATION", "REUSED"]
    summary: str
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    actual_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    status: Literal["PASS", "REPLAN", "NEEDS_CLARIFICATION", "FAIL"]
    missing_evidence: list[str] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    geometry_issues: list[str] = Field(default_factory=list)
    provenance_issues: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    recommended_replan_nodes: list[str] = Field(default_factory=list)
    reason: str = ""


class PlanPatch(BaseModel):
    patch_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    graph_id: str
    replace_task_ids: list[str]
    replacement_nodes: list[TaskNode]
    reason: str
    created_by: Literal["orchestrator"] = "orchestrator"
    planner_mode: Literal["deterministic", "llm"] = "deterministic"
    planning_metadata: dict[str, Any] = Field(default_factory=dict)


class LLMPlanPatchDraft(BaseModel):
    """Schema-only local repair proposed by the LLM and governed in code."""

    replace_task_ids: list[str] = Field(min_length=1)
    replacement_nodes: list[LLMTaskNodeDraft] = Field(min_length=1)
    reason: str = Field(min_length=1)


class AgentCapabilityPolicy(BaseModel):
    agent_role: AgentRole
    allowed_agents: tuple[AgentRole, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_read_paths: tuple[str, ...] = ()
    allowed_write_paths: tuple[str, ...] = ()
    allowed_providers: tuple[str, ...] = ()
    allowed_artifact_types: tuple[str, ...] = ()
    max_tool_calls: int = Field(default=20, ge=0)
    max_runtime_seconds: float = Field(default=300, gt=0)
    can_delegate: bool = False
    can_commit_final: bool = False


class PolicyDecision(BaseModel):
    allowed: bool
    error_code: str | None = None
    agent_id: str
    job_id: str
    attempted_action: str
    path: str | None = None
    reason: str
    created_at: datetime = Field(default_factory=utc_now)


class ConflictRecord(BaseModel):
    conflict_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: Literal[
        "goal_constraint",
        "contradictory_evidence",
        "duplicate_ownership",
        "crs_mismatch",
        "provider_disagreement",
    ]
    parties: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    resolution: Literal["DETERMINISTIC", "REVIEW", "NEEDS_CLARIFICATION", "DEGRADED"]
    reason: str


class OrchestrationResult(BaseModel):
    job_id: str
    intent: GeoIntentDecision
    goal_contract: GoalContract
    task_graph: TaskGraph | None = None
    agent_results: list[AgentResult] = Field(default_factory=list)
    review: ReviewResult | None = None
    replans: list[PlanPatch] = Field(default_factory=list)
    final_artifacts: list[ArtifactRef] = Field(default_factory=list)
    status: Literal["PLANNED", "SUCCEEDED", "FAILED", "NEEDS_CLARIFICATION"]
    metrics: dict[str, Any] = Field(default_factory=dict)

"""Request-dependent task graph planner and finite replan guards."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from geoagent.orchestration.models import GoalContract, LLMPlanPatchDraft, LLMTaskGraphDraft, PlanPatch, ReviewResult, TaskGraph, TaskNode, stable_hash
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM


TOOLS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "data": ("search_catalog", "resolve_boundary", "acquire_aoi_window", "provider_cache_lookup"),
    "gis_analysis": ("buffer", "intersect", "clip", "dissolve", "spatial_join", "zonal_statistics", "nearest_distance", "spatial_exclusion"),
    "terrain": ("acquire_dem", "calculate_slope", "calculate_aspect", "terrain_statistics", "basic_hydrology"),
    "remote_sensing": ("search_sentinel", "select_bands", "read_raster_window", "calculate_ndvi", "create_rgb_chip", "optional_vlm_verify"),
    "renewable_siting": ("score_candidates", "rank_candidates", "capacity_area_check"),
    "map_report": ("add_result_to_map", "render_map", "generate_report"),
    "reviewer": ("validate_schema", "validate_geometry", "validate_provenance", "check_goal_constraints"),
}

MAX_SPECIALIST_TOOL_CALLS = 12


def _normalize_tool_name(role: str, raw: str) -> str:
    """Map only an unambiguous provider alias onto the existing allow-list."""
    allowed = TOOLS_BY_ROLE[role]
    normalized = raw.strip().strip("`\"'").lower().replace("-", "_").replace(" ", "_")
    if normalized in allowed:
        return normalized
    for prefix in (f"{role}_", "tool_", "geo_"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    for suffix in ("_tool", "_operation", "_analysis"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    if normalized in allowed:
        return normalized
    candidates = [name for name in allowed if name in normalized]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"Ambiguous or unknown {role} tool alias: {raw!r}")

REQUIRED_ROLES_BY_INTENT: dict[str, set[str]] = {
    "general_gis": {"gis_analysis"},
    "data_discovery": {"data"},
    "terrain_analysis": {"data", "terrain"},
    "remote_sensing": {"data", "remote_sensing"},
    "renewable_siting": {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer"},
    "map_visualization": {"map_report"},
    "multi_domain": {"data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"},
}


def _node(contract: GoalContract, task_id: str, role: str, objective: str, *, deps: list[str] | None = None, inputs: list[str] | None = None, outputs: list[str] | None = None, tools: list[str] | None = None, cost: str = "low") -> TaskNode:
    allowed = list(TOOLS_BY_ROLE[role])
    selected = tools or allowed[:1]
    invalid = set(selected) - set(allowed)
    if invalid:
        raise ValueError(f"Tools outside {role} surface: {sorted(invalid)}")
    return TaskNode(
        task_id=task_id,
        goal_id=contract.goal_id,
        agent_role=role,  # type: ignore[arg-type]
        objective=objective,
        dependencies=deps or [],
        required_inputs=inputs or [],
        expected_outputs=outputs or [f"{task_id}_artifact"],
        allowed_tools=selected,
        success_criteria=["schema_valid", "provenance_present", "goal_contract_unchanged"],
        cost_hint=cost,  # type: ignore[arg-type]
    ).with_signature(contract)


@dataclass
class DynamicGeoPlanner:
    max_graph_depth: int = 8
    max_nodes: int = 24
    llm: StrandsStructuredLLM | None = None
    max_attempts: int = 2
    last_llm_error: str | None = field(default=None, init=False)

    def create(
        self,
        contract: GoalContract,
        *,
        existing_artifacts: list[dict[str, Any]] | None = None,
        existing_evidence: list[dict[str, Any]] | None = None,
        provider_status: dict[str, Any] | None = None,
        execution_state: dict[str, Any] | None = None,
        execution_budget: dict[str, Any] | None = None,
        use_llm: bool = True,
    ) -> TaskGraph:
        """Generate an LLM plan first and fall back only on unavailable/invalid LLM output."""
        active = self.llm if self.llm is not None else (StrandsStructuredLLM.from_environment() if use_llm else None)
        if use_llm and active is None:
            self.last_llm_error = StrandsStructuredLLM.last_environment_error
        required_roles = self._required_roles(contract)
        if active is not None:
            context = {
                "goal_contract": contract.model_dump(mode="json"),
                "available_agents_and_tools": {role: list(tools) for role, tools in TOOLS_BY_ROLE.items()},
                "required_agent_roles": sorted(required_roles),
                "existing_artifacts": (existing_artifacts or [])[:50],
                "existing_evidence": (existing_evidence or [])[:50],
                "provider_status": provider_status or {},
                "execution_state": execution_state or {},
                "execution_budget": execution_budget or {
                    "max_graph_depth": self.max_graph_depth,
                    "max_nodes": self.max_nodes,
                    "max_tool_calls_per_specialist_role": MAX_SPECIALIST_TOOL_CALLS,
                },
            }
            prompt = (
                "Create a request-specific geospatial TaskGraph. Return only the requested structured "
                "draft. Do not emit a fixed workflow. Include only necessary specialist agents and "
                "their allow-listed tools; never select orchestrator as a task agent. Dependencies "
                "must form an acyclic graph, independent work should be parallel, valid existing "
                "artifacts/evidence should be reused, and a reviewer must not execute domain tools. "
                "Use the smallest sufficient graph; normally create one task per selected specialist "
                "role and keep the graph at or below 10 nodes unless the goal truly requires more. "
                "Select only tools that each task will actually call, and keep the sum of selected "
                "tools across all tasks for one specialist role at or below 12. "
                "For reviewer nodes, required_inputs must be dependency task_ids, not human-readable "
                "artifact labels. "
                "Never change the immutable GoalContract.\nPLANNING CONTEXT:\n"
                + json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
            )
            for _ in range(self.max_attempts):
                try:
                    draft = active.generate(prompt, LLMTaskGraphDraft)
                    return self._govern_llm_draft(contract, draft, existing_artifacts, active, required_roles)
                except Exception as exc:
                    self.last_llm_error = f"{type(exc).__name__}: {exc}"[:2000]
                    prompt = (
                        prompt
                        + "\nPREVIOUS DRAFT REJECTED BY GOVERNANCE:\n"
                        + self.last_llm_error
                        + "\nReturn a corrected smaller draft. Do not repeat the rejected tool allocation."
                    )
                    continue
        graph = self._create_deterministic(contract, existing_artifacts=existing_artifacts, provider_status=provider_status, execution_budget=execution_budget)
        graph.planning_metadata = {
            "mode": "deterministic_fallback" if use_llm else "deterministic_explicit",
            "llm_available": active is not None,
            "llm_error": self.last_llm_error,
        }
        return graph

    def _govern_llm_draft(
        self,
        contract: GoalContract,
        draft: LLMTaskGraphDraft,
        existing_artifacts: list[dict[str, Any]] | None,
        active: StrandsStructuredLLM,
        required_roles: set[str] | None = None,
    ) -> TaskGraph:
        if len(draft.nodes) > self.max_nodes:
            raise ValueError("Planner node budget exceeded")
        actual_roles = {item.agent_role for item in draft.nodes}
        if "orchestrator" in actual_roles:
            raise ValueError("Orchestrator cannot be a specialist task node")
        missing_roles = (required_roles if required_roles is not None else self._required_roles(contract)) - actual_roles
        insert_data_node = missing_roles == {"data"}
        if missing_roles and not insert_data_node:
            raise ValueError(f"LLM plan omitted required roles: {sorted(missing_roles)}")
        role_tool_calls: dict[str, int] = {}
        for item in draft.nodes:
            role_tool_calls[item.agent_role] = role_tool_calls.get(item.agent_role, 0) + len(item.allowed_tools)
        excessive_roles = {
            role: count
            for role, count in role_tool_calls.items()
            if role != "reviewer" and count > MAX_SPECIALIST_TOOL_CALLS
        }
        if excessive_roles:
            raise ValueError(f"LLM plan exceeds specialist tool budgets: {excessive_roles}")
        governance_corrections: list[dict[str, Any]] = []
        nodes: list[TaskNode] = []
        governed_data_id = "governed-data-context"
        if insert_data_node:
            selected_data_tool = "search_catalog" if contract.intent in {"remote_sensing", "data_discovery"} else "acquire_aoi_window"
            nodes.append(_node(
                contract, governed_data_id, "data",
                "Resolve the required AOI catalog/cache context omitted by the LLM draft",
                outputs=["data_artifact"], tools=[selected_data_tool], cost="low",
            ))
            governance_corrections.append({
                "action": "insert_required_data_context",
                "reason": "GoalContract required DataAgent but the LLM draft omitted only that role",
                "task_id": governed_data_id, "tool": selected_data_tool,
            })
        for item in draft.nodes:
            allowed_surface = set(TOOLS_BY_ROLE[item.agent_role])
            normalized_tools: list[str] = []
            dropped_tools: list[str] = []
            for name in item.allowed_tools:
                try:
                    normalized_tools.append(_normalize_tool_name(item.agent_role, name))
                except ValueError:
                    dropped_tools.append(name)
            if dropped_tools:
                governance_corrections.append({
                    "task_id": item.task_id, "action": "drop_cross_role_or_unknown_tools",
                    "dropped_tools": dropped_tools,
                })
            if not normalized_tools or not set(normalized_tools).issubset(allowed_surface):
                raise ValueError(f"LLM selected tools outside {item.agent_role} capability: {item.allowed_tools!r}")
            # Harness inputs are materialized from dependency task ids.  Keep
            # rich semantic labels for domain nodes, but normalize reviewer
            # requirements to those durable ids so a valid artifact is not
            # rejected merely because the LLM used a translated display name.
            dependencies = list(item.dependencies)
            if insert_data_node and item.agent_role in {"terrain", "remote_sensing", "gis_analysis", "renewable_siting"} and not dependencies:
                dependencies = [governed_data_id]
            required_inputs = dependencies if item.agent_role == "reviewer" else item.required_inputs
            nodes.append(
                TaskNode(
                    task_id=item.task_id,
                    goal_id=contract.goal_id,
                    agent_role=item.agent_role,
                    objective=item.objective,
                    dependencies=dependencies,
                    required_inputs=required_inputs,
                    expected_outputs=item.expected_outputs or [f"{item.task_id}_artifact"],
                    allowed_tools=normalized_tools,
                    success_criteria=item.success_criteria or ["schema_valid", "provenance_present", "goal_contract_unchanged"],
                    cost_hint=item.cost_hint,
                ).with_signature(contract)
            )
        self._validate_depth(nodes)
        graph = TaskGraph(
            goal_id=contract.goal_id,
            nodes=nodes,
            planner_mode="llm",
            planning_metadata={
                "mode": "llm",
                "llm_available": True,
                "provider": getattr(active, "provider", "unknown"),
                "model": getattr(active, "model_id", None),
                "llm_call_count": getattr(active, "call_count", None),
                "rationale": draft.rationale,
                "governance_corrections": governance_corrections,
            },
        )
        if existing_artifacts:
            signatures = {item.get("task_signature") for item in existing_artifacts if item.get("valid", True)}
            graph.nodes = [node.model_copy(update={"metadata": {**node.metadata, "reuse_candidate": node.task_signature in signatures}}) for node in graph.nodes]
        return graph

    def _create_deterministic(self, contract: GoalContract, *, existing_artifacts: list[dict[str, Any]] | None = None, provider_status: dict[str, Any] | None = None, execution_budget: dict[str, Any] | None = None) -> TaskGraph:
        query = contract.primary_goal.lower()
        nodes: list[TaskNode] = []
        if contract.intent == "general_gis":
            operations = [name for name in ("buffer", "intersect", "clip", "dissolve", "spatial_join", "zonal_statistics") if name.replace("_", " ") in query or {"buffer": "缓冲", "intersect": "相交", "clip": "裁剪", "dissolve": "融合", "spatial_join": "空间连接", "zonal_statistics": "分区统计"}[name] in query]
            if any(term in query for term in ("distance", "proximity", "距离", "邻近")):
                operations.append("nearest_distance")
            operations = operations or ["clip"]
            previous: str | None = None
            for index, operation in enumerate(operations, 1):
                task_id = f"gis-{index}-{operation}"
                nodes.append(_node(contract, task_id, "gis_analysis", f"Apply {operation} using user inputs", deps=[previous] if previous else [], tools=[operation]))
                previous = task_id
            if "map" in query or "地图" in query:
                nodes.append(_node(contract, "map-result", "map_report", "Add the GIS result to the current map", deps=[previous] if previous else [], tools=["add_result_to_map"]))
        elif contract.intent == "data_discovery":
            nodes.append(_node(contract, "discover-data", "data", "Discover the requested AOI dataset and provenance", tools=["search_catalog"], cost="medium"))
            if "map" in query or "地图" in query:
                nodes.append(_node(contract, "map-data", "map_report", "Visualize discovered data footprint", deps=["discover-data"], tools=["add_result_to_map"]))
        elif contract.intent == "terrain_analysis":
            nodes.extend([
                _node(contract, "terrain-data", "data", "Resolve AOI and acquire only the required DEM window", tools=["acquire_aoi_window"], cost="medium"),
                _node(contract, "terrain-analysis", "terrain", "Calculate requested terrain evidence", deps=["terrain-data"], inputs=["terrain-data_artifact"], tools=["calculate_aspect" if "aspect" in query or "坡向" in query else "calculate_slope"], cost="medium"),
            ])
        elif contract.intent == "remote_sensing":
            nodes.extend([
                _node(contract, "stac-search", "data", "Search current AOI STAC items without full-scene download", tools=["search_catalog"], cost="medium"),
                _node(contract, "remote-analysis", "remote_sensing", "Read only required bands/windows and compute requested evidence", deps=["stac-search"], tools=["calculate_ndvi" if "ndvi" in query or "植被指数" in query else "create_rgb_chip"], cost="medium"),
            ])
        elif contract.intent == "renewable_siting":
            nodes.extend([
                _node(contract, "siting-data", "data", "Acquire boundary, resource and AOI data with cache/provider provenance", tools=["acquire_aoi_window"], cost="medium"),
                _node(contract, "siting-terrain", "terrain", "Produce slope and terrain evidence", deps=["siting-data"], tools=["calculate_slope"], cost="medium"),
                _node(contract, "siting-gis", "gis_analysis", "Produce water, land and infrastructure spatial evidence", deps=["siting-data"], tools=["spatial_exclusion"], cost="medium"),
                _node(contract, "siting-score", "renewable_siting", "Apply domain scoring without duplicating shared GIS operations", deps=["siting-terrain", "siting-gis"], tools=["score_candidates"]),
                _node(contract, "siting-review", "reviewer", "Read-only validation of constraints, geometry and provenance", deps=["siting-score"], tools=["check_goal_constraints"]),
            ])
        elif contract.intent == "map_visualization":
            nodes.append(_node(contract, "map-only", "map_report", "Render or update the requested map", tools=["add_result_to_map"]))
        elif contract.intent == "multi_domain":
            regions = contract.region_or_aoi.get("regions", []) if isinstance(contract.region_or_aoi, dict) else []
            comparative = any(term in query for term in ("compare", "comparison", "versus", " vs ", "比较", "对比", "哪个地区"))
            if comparative and len(regions) >= 2:
                terminal_nodes: list[str] = []
                for index, region in enumerate(regions, 1):
                    data_id = f"compare-{index}-data"
                    terrain_id = f"compare-{index}-terrain"
                    infra_id = f"compare-{index}-infrastructure"
                    nodes.extend([
                        _node(contract, data_id, "data", f"Acquire comparable evidence for {region}", tools=["acquire_aoi_window"], cost="medium"),
                        _node(contract, terrain_id, "terrain", f"Analyze terrain for {region}", deps=[data_id], tools=["terrain_statistics"]),
                        _node(contract, infra_id, "gis_analysis", f"Analyze infrastructure proximity for {region}", deps=[data_id], tools=["nearest_distance"]),
                    ])
                    terminal_nodes.extend([terrain_id, infra_id])
                nodes.extend([
                    _node(contract, "compare-domain", "renewable_siting", "Compare evidence without changing the user goal", deps=terminal_nodes, tools=["rank_candidates"]),
                    _node(contract, "compare-review", "reviewer", "Resolve conflicts and validate comparability", deps=["compare-domain"], inputs=["compare-domain"], tools=["validate_provenance"]),
                    _node(contract, "compare-report", "map_report", "Create concise comparison and map evidence", deps=["compare-review"], tools=["generate_report"]),
                ])
            else:
                required_roles = self._required_roles(contract)
                data_id = "multi-data"
                if "data" in required_roles:
                    nodes.append(_node(contract, data_id, "data", "Resolve AOI and accepted data/provider context", tools=["acquire_aoi_window"], cost="medium"))
                domain_nodes: list[str] = []
                dependencies = [data_id] if "data" in required_roles else []
                if "terrain" in required_roles:
                    nodes.append(_node(contract, "multi-terrain", "terrain", "Produce terrain, elevation and slope evidence", deps=dependencies, tools=["terrain_statistics"], cost="medium"))
                    domain_nodes.append("multi-terrain")
                if "gis_analysis" in required_roles:
                    nodes.append(_node(contract, "multi-gis", "gis_analysis", "Analyze requested road/accessibility spatial evidence", deps=dependencies, tools=["nearest_distance"], cost="medium"))
                    domain_nodes.append("multi-gis")
                if "remote_sensing" in required_roles:
                    nodes.append(_node(contract, "multi-remote", "remote_sensing", "Search Sentinel metadata and assess imagery availability without full-scene download", deps=dependencies, tools=["search_sentinel"], cost="medium"))
                    domain_nodes.append("multi-remote")
                if "renewable_siting" in required_roles:
                    nodes.append(_node(contract, "multi-renewable", "renewable_siting", "Assess requested renewable/resource evidence", deps=domain_nodes or dependencies, tools=["rank_candidates"], cost="medium"))
                    domain_nodes.append("multi-renewable")
                review_id: str | None = None
                if "reviewer" in required_roles:
                    review_id = "multi-review"
                    nodes.append(_node(contract, review_id, "reviewer", "Validate complete evidence coverage for every requested domain", deps=domain_nodes, inputs=domain_nodes, tools=["validate_provenance"]))
                if "map_report" in required_roles:
                    report_deps = [review_id] if review_id else domain_nodes
                    nodes.append(_node(contract, "multi-report", "map_report", "Generate a concise source-aware user summary", deps=report_deps, tools=["generate_report"]))
        else:
            raise ValueError("Unknown or incomplete goal cannot be planned")
        if len(nodes) > self.max_nodes:
            raise ValueError("Planner node budget exceeded")
        self._validate_depth(nodes)
        graph = TaskGraph(goal_id=contract.goal_id, nodes=nodes)
        if existing_artifacts:
            signatures = {item.get("task_signature") for item in existing_artifacts if item.get("valid", True)}
            graph.nodes = [node.model_copy(update={"metadata": {**node.metadata, "reuse_candidate": node.task_signature in signatures}}) for node in graph.nodes]
        return graph

    @staticmethod
    def _required_roles(contract: GoalContract) -> set[str]:
        if contract.intent != "multi_domain":
            return set(REQUIRED_ROLES_BY_INTENT.get(contract.intent, set()))
        # Multi-domain requirements are query-dependent.  Reuse the governed
        # deterministic capability matcher so terrain + remote sensing does
        # not expand into a fixed renewable workflow, while explicit domains
        # can never overwrite one another.
        from geoagent.orchestration.routing import deterministic_route

        roles = set(deterministic_route(contract.primary_goal).required_agent_roles)
        roles.add("reviewer")
        return roles

    def _validate_depth(self, nodes: list[TaskNode]) -> None:
        by_id = {node.task_id: node for node in nodes}
        cache: dict[str, int] = {}
        def depth(task_id: str) -> int:
            if task_id in cache:
                return cache[task_id]
            deps = by_id[task_id].dependencies
            value = 1 if not deps else 1 + max(depth(parent) for parent in deps)
            cache[task_id] = value
            return value
        if max((depth(node.task_id) for node in nodes), default=0) > self.max_graph_depth:
            raise ValueError("TaskGraph depth budget exceeded")

    def replan(
        self,
        *,
        contract: GoalContract,
        graph: TaskGraph,
        review: ReviewResult,
        completed_task_ids: set[str],
        evidence: list[dict[str, Any]],
        execution_state: dict[str, Any],
        use_llm: bool = True,
    ) -> PlanPatch:
        """Ask the LLM for a bounded local patch; preserve successful nodes."""
        affected = list(dict.fromkeys(review.recommended_replan_nodes))
        if not affected:
            raise RuntimeError("REPLAN_REQUESTED_WITHOUT_AFFECTED_NODES")
        if set(affected) & completed_task_ids:
            raise ValueError("Replan cannot replace successful nodes")
        active = self.llm if self.llm is not None else (StrandsStructuredLLM.from_environment() if use_llm else None)
        if active is not None:
            context = {
                "goal_contract": contract.model_dump(mode="json"),
                "previous_task_graph": graph.model_dump(mode="json"),
                "completed_task_ids": sorted(completed_task_ids),
                "failed_or_affected_task_ids": affected,
                "review": review.model_dump(mode="json"),
                "evidence": evidence[-50:],
                "execution_state": execution_state,
                "available_agents_and_tools": {role: list(tools) for role, tools in TOOLS_BY_ROLE.items()},
            }
            prompt = (
                "Return a local PlanPatch only. Replace exactly the failed/affected task ids, keep "
                "each replacement task_id and agent_role unchanged, never replace or rerun a successful "
                "node, select only allow-listed tools, preserve the immutable GoalContract, and keep all "
                "unaffected dependencies valid.\nREPLAN CONTEXT:\n"
                + json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
            )
            for _ in range(self.max_attempts):
                try:
                    draft = active.generate(prompt, LLMPlanPatchDraft)
                    if set(draft.replace_task_ids) != set(affected):
                        raise ValueError("LLM patch changed affected task scope")
                    by_id = {node.task_id: node for node in graph.nodes}
                    if {node.task_id for node in draft.replacement_nodes} != set(affected):
                        raise ValueError("LLM patch must preserve replacement task ids")
                    replacements: list[TaskNode] = []
                    for item in draft.replacement_nodes:
                        prior = by_id[item.task_id]
                        if item.agent_role != prior.agent_role:
                            raise ValueError("LLM patch cannot change specialist ownership")
                        normalized_tools = [_normalize_tool_name(item.agent_role, name) for name in item.allowed_tools]
                        if not normalized_tools or not set(normalized_tools).issubset(set(TOOLS_BY_ROLE[item.agent_role])):
                            raise ValueError("LLM patch selected tools outside specialist capability")
                        replacements.append(TaskNode(
                            task_id=item.task_id, goal_id=contract.goal_id, agent_role=item.agent_role,
                            objective=item.objective, dependencies=item.dependencies,
                            required_inputs=item.dependencies if item.agent_role == "reviewer" else item.required_inputs,
                            expected_outputs=item.expected_outputs or prior.expected_outputs,
                            allowed_tools=normalized_tools, success_criteria=item.success_criteria or prior.success_criteria,
                            cost_hint=item.cost_hint, metadata={**prior.metadata, "local_replan": True},
                        ).with_signature(contract))
                    candidate = TaskGraph(goal_id=graph.goal_id, nodes=[node for node in graph.nodes if node.task_id not in set(affected)] + replacements, planner_mode=graph.planner_mode)
                    self._validate_depth(candidate.nodes)
                    return PlanPatch(
                        graph_id=graph.graph_id, replace_task_ids=affected, replacement_nodes=replacements,
                        reason=draft.reason, planner_mode="llm",
                        planning_metadata={
                            "provider": getattr(active, "provider", "unknown"),
                            "model": getattr(active, "model_id", None),
                            "llm_call_count": getattr(active, "call_count", None),
                        },
                    )
                except Exception as exc:
                    self.last_llm_error = f"{type(exc).__name__}: {exc}"[:2000]
                    prompt = (
                        prompt
                        + "\nPREVIOUS PATCH REJECTED BY GOVERNANCE:\n"
                        + self.last_llm_error
                        + "\nReturn a corrected local patch within the same affected-node scope."
                    )
        replacements = []
        for task_id in affected:
            node = graph.node(task_id)
            replacements.append(node.model_copy(update={
                "objective": f"{node.objective} [deterministic local repair: {review.reason}]",
                "task_signature": "", "metadata": {**node.metadata, "local_replan": True},
            }).with_signature(contract))
        return PlanPatch(
            graph_id=graph.graph_id, replace_task_ids=affected, replacement_nodes=replacements,
            reason=review.reason, planner_mode="deterministic",
            planning_metadata={"mode": "deterministic_fallback" if use_llm else "deterministic_explicit", "llm_error": self.last_llm_error},
        )

    @staticmethod
    def apply_patch(graph: TaskGraph, patch: PlanPatch, succeeded: set[str]) -> TaskGraph:
        if set(patch.replace_task_ids) & succeeded:
            raise ValueError("Replan cannot replace successful nodes")
        retained = [node for node in graph.nodes if node.task_id not in set(patch.replace_task_ids)]
        return TaskGraph(
            goal_id=graph.goal_id,
            nodes=retained + patch.replacement_nodes,
            planner_mode=graph.planner_mode,
            planning_metadata={**graph.planning_metadata, "replanned_from_graph_id": graph.graph_id},
        )


@dataclass
class NoProgressGuard:
    threshold: int = 2
    records: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)

    def record(self, plan_hash: str, evidence_ids: list[str], state_delta: dict[str, Any]) -> None:
        current = (plan_hash, tuple(sorted(evidence_ids)), stable_hash(state_delta))
        self.records.append(current)
        if len(self.records) >= self.threshold and len(set(self.records[-self.threshold:])) == 1:
            raise RuntimeError("NO_PROGRESS_LOOP")

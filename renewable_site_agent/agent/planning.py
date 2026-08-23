"""Finite LLM planner with schema and skill allow-list validation."""

from __future__ import annotations

from dataclasses import dataclass

from renewable_site_agent.prompts import PLANNER_PROMPT, PROMPT_VERSION
from renewable_site_agent.schemas import SiteSelectionRequest, ToolPlan, ToolPlanStep

from .skills import SkillRegistry
from .structured_llm import StrandsStructuredLLM


def deterministic_site_plan(request: SiteSelectionRequest) -> ToolPlan:
    names = ["resolve_administrative_boundary"]
    if request.exclude_water:
        names.append("acquire_water_mask")
    if request.exclude_protected_area:
        names.append("acquire_protected_areas")
    names.extend(["acquire_land_cover", "acquire_dem", "calculate_slope"])
    names.extend(["acquire_power_infrastructure", "acquire_road_network"])
    if request.project_type in {"solar", "wind", "hybrid"}:
        names.append("acquire_energy_resource")
    names.extend(
        [
            "apply_spatial_exclusion",
            "build_candidate_polygons",
            "calculate_nearest_distance",
            "calculate_candidate_statistics",
            "score_candidates",
            "validate_candidate_geometry",
        ]
    )
    if request.require_remote_sensing:
        names.append("verify_candidate_remote_sensing")
    return ToolPlan(
        selected_skill="site-selection",
        planner_mode="deterministic_fallback",
        steps=[
            ToolPlanStep(step_id=f"{index:02d}-{name}", tool_name=name)
            for index, name in enumerate(names, 1)
        ],
    )


@dataclass
class Planner:
    llm: StrandsStructuredLLM | None = None
    max_steps: int = 20
    registry: SkillRegistry = SkillRegistry()

    def create(self, request: SiteSelectionRequest, selected_skill: str) -> ToolPlan:
        allowed = list(self.registry.allowed_tools(selected_skill))
        active_llm = self.llm if self.llm is not None else StrandsStructuredLLM.from_environment()
        if active_llm is not None:
            prompt = PLANNER_PROMPT.format(
                version=PROMPT_VERSION,
                request=request.model_dump_json(),
                allowed_tools=allowed,
                max_steps=self.max_steps,
            )
            for _ in range(2):
                try:
                    plan = active_llm.generate(prompt, ToolPlan)
                    plan.planner_mode = "llm"
                    return self.validate(plan, selected_skill)
                except Exception:
                    continue
        if selected_skill != "site-selection":
            return ToolPlan(
                selected_skill=selected_skill,
                planner_mode="deterministic_fallback",
                steps=[
                    ToolPlanStep(step_id=f"{i:02d}-{name}", tool_name=name)
                    for i, name in enumerate(allowed[: self.max_steps], 1)
                ],
            )
        return self.validate(deterministic_site_plan(request), selected_skill)

    def validate(self, plan: ToolPlan, selected_skill: str) -> ToolPlan:
        if len(plan.steps) > self.max_steps:
            raise ValueError(f"Plan exceeds max_steps={self.max_steps}")
        allowed = set(self.registry.allowed_tools(selected_skill))
        invalid = [step.tool_name for step in plan.steps if step.tool_name not in allowed]
        if invalid:
            raise ValueError(f"Plan contains tools outside skill scope: {invalid}")
        if len({step.step_id for step in plan.steps}) != len(plan.steps):
            raise ValueError("Plan step_id values must be unique")
        plan.selected_skill = selected_skill
        return plan

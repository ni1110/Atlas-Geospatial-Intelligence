"""Skill registry that enforces hierarchical tool exposure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CORE_SITE_TOOLS = (
    "resolve_administrative_boundary",
    "acquire_water_mask",
    "acquire_dem",
    "calculate_slope",
    "acquire_land_cover",
    "acquire_protected_areas",
    "acquire_power_infrastructure",
    "acquire_road_network",
    "acquire_energy_resource",
    "apply_spatial_exclusion",
    "calculate_nearest_distance",
    "build_candidate_polygons",
    "calculate_candidate_statistics",
    "score_candidates",
    "validate_candidate_geometry",
)


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    instruction_path: Path


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
SKILLS: dict[str, SkillSpec] = {
    "site-selection": SkillSpec(
        "site-selection", "Renewable site screening and deterministic ranking",
        CORE_SITE_TOOLS + ("verify_candidate_remote_sensing",),
        SKILLS_ROOT / "site-selection" / "SKILL.md",
    ),
    "terrain-analysis": SkillSpec(
        "terrain-analysis", "DEM, slope, projection, clipping and terrain statistics",
        ("resolve_administrative_boundary", "acquire_dem", "calculate_slope", "calculate_candidate_statistics", "validate_candidate_geometry"),
        SKILLS_ROOT / "terrain-analysis" / "SKILL.md",
    ),
    "remote-sensing": SkillSpec(
        "remote-sensing", "Current-job Sentinel search, chips and optional VLM evidence",
        ("search_sentinel", "create_sentinel_chip", "calculate_image_evidence", "verify_candidate_remote_sensing"),
        SKILLS_ROOT / "remote-sensing" / "SKILL.md",
    ),
    "general-gis": SkillSpec(
        "general-gis", "General data query and bounded deterministic GIS operations",
        ("resolve_administrative_boundary", "apply_spatial_exclusion", "calculate_nearest_distance", "calculate_candidate_statistics", "validate_candidate_geometry"),
        SKILLS_ROOT / "general-gis" / "SKILL.md",
    ),
}


class SkillRegistry:
    def select(self, name: str) -> SkillSpec:
        if name not in SKILLS:
            raise KeyError(f"Unknown skill: {name}")
        spec = SKILLS[name]
        if not spec.instruction_path.is_file():
            raise FileNotFoundError(f"Missing skill instructions: {spec.instruction_path}")
        return spec

    def allowed_tools(self, name: str) -> tuple[str, ...]:
        return self.select(name).allowed_tools

    def exposure_record(
        self, name: str, selected_tools: list[str], all_tools: list[str]
    ) -> dict[str, object]:
        allowed = set(self.allowed_tools(name))
        rejected = sorted(set(all_tools) - allowed)
        invalid = sorted(set(selected_tools) - allowed)
        if invalid:
            raise ValueError(f"Out-of-skill tools rejected: {invalid}")
        return {
            "selected_skill": name,
            "available_tool_count": len(allowed),
            "selected_tool": selected_tools,
            "rejected_tool": rejected,
        }

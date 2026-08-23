"""Tool registry for renewable-site agent planning.

This module defines machine-readable metadata for deterministic GIS tools.
The Agent may use this registry to assemble an execution plan, but the
geospatial results must still be produced by deterministic programs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


ToolCategory = Literal[
    "data_preparation",
    "gis_screening",
    "reporting",
    "validation",
]


@dataclass(frozen=True)
class RegisteredTool:
    """Metadata for one deterministic tool available to the planner."""

    tool_id: str
    name: str
    category: ToolCategory
    module: str
    description: str
    required_inputs: tuple[str, ...]
    produced_outputs: tuple[str, ...]
    deterministic: bool = True
    requires_human_confirmation: bool = False


REGISTERED_TOOLS: tuple[RegisteredTool, ...] = (
    RegisteredTool(
        tool_id="parse_site_selection_request",
        name="Parse site-selection request",
        category="data_preparation",
        module="renewable_site_agent.application.create_job",
        description="Convert a natural-language renewable-site screening request into a structured SiteSelectionJob.",
        required_inputs=("natural_language_request",),
        produced_outputs=("site_selection_job", "job_id"),
    ),
    RegisteredTool(
        tool_id="load_administrative_boundary",
        name="Load administrative boundary",
        category="data_preparation",
        module="renewable_site_agent.application.create_job",
        description="Load the real administrative boundary for the requested region.",
        required_inputs=("job_id", "region"),
        produced_outputs=("administrative_boundary",),
    ),
    RegisteredTool(
        tool_id="exclude_water_bodies",
        name="Exclude water bodies",
        category="gis_screening",
        module="renewable_site_agent.application.create_job",
        description="Subtract real OSM/Geofabrik water polygons from the administrative boundary to create initial candidate polygons.",
        required_inputs=("job_id", "administrative_boundary", "water_layer"),
        produced_outputs=("water_exclusion", "non_water_area", "initial_candidate_sites"),
    ),
    RegisteredTool(
        tool_id="prepare_terrain_dem",
        name="Prepare terrain DEM",
        category="data_preparation",
        module="renewable_site_agent.application.prepare_terrain_dem",
        description="Merge, clip, and reproject Copernicus GLO-30 DEM for the target region.",
        required_inputs=("job_id", "dem_tiles", "administrative_boundary"),
        produced_outputs=("prepared_dem", "terrain_metadata"),
    ),
    RegisteredTool(
        tool_id="calculate_slope",
        name="Calculate slope",
        category="gis_screening",
        module="renewable_site_agent.application.calculate_slope",
        description="Calculate slope degrees and a slope suitability mask from the prepared DEM.",
        required_inputs=("job_id", "prepared_dem", "max_slope_deg"),
        produced_outputs=("slope_raster", "slope_suitable_mask", "slope_metadata"),
    ),
    RegisteredTool(
        tool_id="vectorize_slope_mask",
        name="Vectorize slope mask",
        category="gis_screening",
        module="renewable_site_agent.application.vectorize_slope_mask",
        description="Convert the slope suitability raster mask into a vector polygon layer.",
        required_inputs=("job_id", "slope_suitable_mask"),
        produced_outputs=("slope_suitable_area", "slope_vector_metadata"),
    ),
    RegisteredTool(
        tool_id="apply_slope_screening",
        name="Apply slope screening",
        category="gis_screening",
        module="renewable_site_agent.application.apply_slope_screening",
        description="Intersect candidate polygons with the slope-suitable area.",
        required_inputs=("job_id", "candidate_sites", "slope_suitable_area"),
        produced_outputs=("slope_screened_candidates", "slope_screening_metadata"),
    ),
    RegisteredTool(
        tool_id="prepare_protected_areas",
        name="Prepare protected areas",
        category="data_preparation",
        module="renewable_site_agent.application.prepare_protected_areas",
        description="Prepare protected-area exclusion polygons from available OSM/Geofabrik layers.",
        required_inputs=("job_id", "administrative_boundary", "local_osm_gpkg"),
        produced_outputs=("protected_area_exclusion", "protected_area_metadata"),
    ),
    RegisteredTool(
        tool_id="apply_protected_area_screening",
        name="Apply protected-area screening",
        category="gis_screening",
        module="renewable_site_agent.application.apply_protected_area_screening",
        description="Subtract protected-area exclusion polygons from slope-screened candidates.",
        required_inputs=("job_id", "slope_screened_candidates", "protected_area_exclusion"),
        produced_outputs=("protected_area_screened_candidates", "protected_area_screening_metadata"),
    ),
    RegisteredTool(
        tool_id="finalize_available_data_candidates",
        name="Finalize available-data candidates",
        category="gis_screening",
        module="renewable_site_agent.application.finalize_available_data_candidates",
        description="Finalize available-data preliminary candidate set and record skipped constraints.",
        required_inputs=("job_id", "protected_area_screened_candidates"),
        produced_outputs=("available_data_final_candidates", "screening_decision_log"),
    ),
    RegisteredTool(
        tool_id="build_map_preview",
        name="Build map preview",
        category="reporting",
        module="renewable_site_agent.application.build_map_preview",
        description="Build lightweight map-preview GeoJSON files from verified final candidates.",
        required_inputs=("job_id", "available_data_final_candidates"),
        produced_outputs=("candidate_map_preview", "map_preview_metadata"),
    ),
    RegisteredTool(
        tool_id="generate_available_data_report",
        name="Generate available-data report",
        category="reporting",
        module="renewable_site_agent.application.generate_available_data_report",
        description="Generate an HTML evidence report for the available-data screening result.",
        required_inputs=("job_id", "screening_decision_log", "evidence_records"),
        produced_outputs=("html_report", "report_metadata"),
    ),
    RegisteredTool(
        tool_id="validate_available_data_job",
        name="Validate available-data job",
        category="validation",
        module="renewable_site_agent.application.validate_available_data_job",
        description="Validate final candidates, map preview consistency, overlaps, and skipped constraints.",
        required_inputs=("job_id", "available_data_final_candidates", "map_preview_metadata"),
        produced_outputs=("validation_summary",),
    ),
)


def list_registered_tools() -> list[dict[str, object]]:
    """Return all registered tool metadata as dictionaries."""

    return [asdict(tool) for tool in REGISTERED_TOOLS]


def get_registered_tool(tool_id: str) -> RegisteredTool:
    """Return one registered tool by ID."""

    for tool in REGISTERED_TOOLS:
        if tool.tool_id == tool_id:
            return tool
    raise KeyError(f"Unknown registered tool: {tool_id}")


def list_tool_ids() -> list[str]:
    """Return all registered tool IDs."""

    return [tool.tool_id for tool in REGISTERED_TOOLS]

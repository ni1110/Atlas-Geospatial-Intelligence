"""FastMCP stdio server wrapping the real data and GIS implementations."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from renewable_site_agent.data.manager import DataSourceManager
from renewable_site_agent.gis.engine import GISExecutor
from renewable_site_agent.schemas import ToolResult


mcp = FastMCP("RenewableSite GIS", json_response=True)
data_manager = DataSourceManager()
gis = GISExecutor()


def _dump(result: ToolResult) -> dict[str, Any]:
    return result.model_dump(mode="json")


@mcp.tool()
def resolve_administrative_boundary(region: str) -> dict[str, Any]:
    """Resolve a China administrative region through geoBoundaries and valid cache."""
    return _dump(data_manager.acquire("administrative_boundary", {"region": region}))


@mcp.tool()
def acquire_water_mask(bbox: list[float], occurrence_threshold: float = 50) -> dict[str, Any]:
    """Resolve AOI-scoped JRC surface-water data, with explicit OSM fallback."""
    return _dump(data_manager.acquire("water", {"bbox": bbox, "occurrence_threshold": occurrence_threshold, "materialize": True}))


@mcp.tool()
def acquire_dem(bbox: list[float]) -> dict[str, Any]:
    """Resolve only Copernicus GLO-30 COG tiles intersecting the AOI."""
    return _dump(data_manager.acquire("dem", {"bbox": bbox, "materialize": True}))


@mcp.tool()
def calculate_slope(dem_path: str, output_path: str, max_slope_deg: float) -> dict[str, Any]:
    """Calculate slope degrees from a metric-CRS DEM raster."""
    return _dump(gis.calculate_slope(dem_path, output_path, max_slope_deg))


@mcp.tool()
def acquire_land_cover(bbox: list[float]) -> dict[str, Any]:
    """Resolve only ESA WorldCover COG tiles intersecting the AOI."""
    return _dump(data_manager.acquire("land_cover", {"bbox": bbox, "materialize": True}))


@mcp.tool()
def acquire_protected_areas(bbox: list[float]) -> dict[str, Any]:
    """Resolve protected areas, identifying OSM data as a non-legal proxy."""
    return _dump(data_manager.acquire("protected_areas", {"bbox": bbox}))


@mcp.tool()
def acquire_power_infrastructure(bbox: list[float]) -> dict[str, Any]:
    """Query AOI-scoped OSM substations and power lines as infrastructure proxy data."""
    return _dump(data_manager.acquire("power_infrastructure", {"bbox": bbox}))


@mcp.tool()
def acquire_road_network(bbox: list[float]) -> dict[str, Any]:
    """Query AOI-scoped OSM major roads, excluding footways and paths."""
    return _dump(data_manager.acquire("roads", {"bbox": bbox}))


@mcp.tool()
def acquire_energy_resource(bbox: list[float], project_type: str) -> dict[str, Any]:
    """Query NASA POWER solar or wind climatology with units and resolution metadata."""
    return _dump(data_manager.acquire("energy_resource", {"bbox": bbox, "project_type": project_type}))


@mcp.tool()
def apply_spatial_exclusion(
    aoi_geojson: dict[str, Any],
    exclusions_geojson: list[dict[str, Any]],
    raster_exclusions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Subtract hard-constraint polygons from an AOI."""
    return _dump(gis.apply_spatial_exclusion(aoi_geojson, exclusions_geojson, raster_exclusions))


@mcp.tool()
def calculate_nearest_distance(
    candidates_geojson: dict[str, Any], target_geojson: dict[str, Any], property_name: str
) -> dict[str, Any]:
    """Calculate candidate-to-target distances in a local projected CRS, in kilometres."""
    return _dump(gis.calculate_nearest_distance(candidates_geojson, target_geojson, property_name))


@mcp.tool()
def build_candidate_polygons(
    suitable_geojson: dict[str, Any], minimum_area_km2: float = 0.01
) -> dict[str, Any]:
    """Clean, dissolve, split, and remove sliver candidate polygons."""
    return _dump(gis.build_candidate_polygons(suitable_geojson, minimum_area_km2))


@mcp.tool()
def calculate_candidate_statistics(
    candidates_geojson: dict[str, Any],
    slope_raster_path: str | None = None,
    land_cover_raster_path: str | None = None,
    land_cover_preferences: dict[str, float] | None = None,
    energy_resource_score: float | None = None,
) -> dict[str, Any]:
    """Calculate per-candidate slope, land-cover, and energy evidence."""
    return _dump(
        gis.calculate_candidate_statistics(
            candidates_geojson,
            slope_raster_path,
            land_cover_raster_path,
            land_cover_preferences,
            energy_resource_score,
        )
    )


@mcp.tool()
def validate_candidate_geometry(
    candidates_geojson: dict[str, Any],
    aoi_geojson: dict[str, Any],
    prohibited_geojson: list[dict[str, Any]] | None = None,
    minimum_area_km2: float | None = None,
) -> dict[str, Any]:
    """Validate candidate geometry, AOI containment, exclusions, duplicates, and area."""
    return _dump(
        gis.validate_candidate_geometry(
            candidates_geojson, aoi_geojson, prohibited_geojson, minimum_area_km2
        )
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Deterministic and validated polygon difference operations."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import geopandas as gpd
from shapely import make_valid
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
)


class SpatialDifferenceError(RuntimeError):
    """Raised when a validated polygon difference cannot be produced."""


@dataclass(frozen=True)
class DifferenceResult:
    artifact_path: Path
    part_count: int
    boundary_area_km2: float
    excluded_area_km2: float
    result_area_km2: float
    overlap_area_km2: float
    conservation_error_km2: float
    metric_crs: str


def _polygon_parts(geometry: object) -> Iterator[Polygon]:
    """Yield Polygon parts from a Shapely geometry."""
    if isinstance(geometry, Polygon):
        yield geometry
        return

    if isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
        return

    if isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from _polygon_parts(item)


class SpatialDifferenceEngine:
    """Subtract an exclusion layer from a boundary with validation."""

    def subtract(
        self,
        *,
        boundary_path: Path,
        exclusion_path: Path,
        output_path: Path,
    ) -> DifferenceResult:
        if not boundary_path.exists():
            raise SpatialDifferenceError(
                f"Boundary file not found: {boundary_path}"
            )

        if not exclusion_path.exists():
            raise SpatialDifferenceError(
                f"Exclusion file not found: {exclusion_path}"
            )

        boundary = gpd.read_file(boundary_path)
        exclusion = gpd.read_file(exclusion_path)

        if boundary.empty:
            raise SpatialDifferenceError("Boundary layer is empty.")

        if exclusion.empty:
            raise SpatialDifferenceError("Exclusion layer is empty.")

        if boundary.crs is None:
            boundary = boundary.set_crs("EPSG:4326")

        if exclusion.crs is None:
            exclusion = exclusion.set_crs("EPSG:4326")

        metric_crs = boundary.estimate_utm_crs()

        if metric_crs is None:
            raise SpatialDifferenceError(
                "Unable to determine projected CRS."
            )

        boundary_metric = boundary.to_crs(metric_crs)
        exclusion_metric = exclusion.to_crs(metric_crs)

        boundary_metric["geometry"] = (
            boundary_metric.geometry.make_valid()
        )
        exclusion_metric["geometry"] = (
            exclusion_metric.geometry.make_valid()
        )

        boundary_union = make_valid(
            boundary_metric.geometry.union_all()
        )

        exclusion_union = make_valid(
            exclusion_metric.geometry.union_all()
        )

        # Only exclusion geometry inside the administrative boundary counts.
        exclusion_inside = make_valid(
            exclusion_union.intersection(boundary_union)
        )

        result_geometry = make_valid(
            boundary_union.difference(exclusion_inside)
        )

        parts = [
            part
            for part in _polygon_parts(result_geometry)
            if not part.is_empty and part.area > 0
        ]

        if not parts:
            raise SpatialDifferenceError(
                "Difference operation produced no polygon result."
            )

        result = gpd.GeoDataFrame(
            {
                "part_id": [
                    f"non-water-{index:05d}"
                    for index in range(1, len(parts) + 1)
                ],
            },
            geometry=parts,
            crs=metric_crs,
        )

        result["area_km2"] = (
            result.geometry.area / 1_000_000
        )

        result = result.sort_values(
            "area_km2",
            ascending=False,
        ).reset_index(drop=True)

        boundary_area_km2 = float(
            boundary_union.area / 1_000_000
        )
        excluded_area_km2 = float(
            exclusion_inside.area / 1_000_000
        )
        result_area_km2 = float(
            result.geometry.area.sum() / 1_000_000
        )

        result_union = make_valid(
            result.geometry.union_all()
        )

        overlap_area_km2 = float(
            result_union.intersection(exclusion_inside).area
            / 1_000_000
        )

        conservation_error_km2 = abs(
            boundary_area_km2
            - excluded_area_km2
            - result_area_km2
        )

        tolerance_km2 = max(
            boundary_area_km2 * 1e-6,
            0.001,
        )

        if overlap_area_km2 > tolerance_km2:
            raise SpatialDifferenceError(
                "Result still overlaps the exclusion layer: "
                f"{overlap_area_km2:.6f} km²."
            )

        if conservation_error_km2 > tolerance_km2:
            raise SpatialDifferenceError(
                "Area conservation validation failed: "
                f"{conservation_error_km2:.6f} km²."
            )

        result["source_operation"] = "boundary_difference_water"
        result["source_boundary"] = str(boundary_path)
        result["source_exclusion"] = str(exclusion_path)

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = output_path.with_name(
            output_path.stem + ".tmp.geojson"
        )
        temporary_path.unlink(missing_ok=True)

        result.to_crs("EPSG:4326").to_file(
            temporary_path,
            driver="GeoJSON",
        )

        temporary_path.replace(output_path)

        return DifferenceResult(
            artifact_path=output_path,
            part_count=len(result),
            boundary_area_km2=boundary_area_km2,
            excluded_area_km2=excluded_area_km2,
            result_area_km2=result_area_km2,
            overlap_area_km2=overlap_area_km2,
            conservation_error_km2=conservation_error_km2,
            metric_crs=metric_crs.to_string(),
        )

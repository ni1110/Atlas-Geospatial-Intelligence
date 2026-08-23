"""Extract real OSM water polygons within a job boundary."""

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd


class WaterExtractionError(RuntimeError):
    """Raised when the water exclusion layer cannot be produced."""


@dataclass(frozen=True)
class WaterExclusionResult:
    artifact_path: Path
    feature_count: int
    total_area_km2: float
    input_feature_count: int
    source_layer: str
    metric_crs: str


class GeofabrikWaterProvider:
    """Read water polygons from a local Geofabrik GeoPackage."""

    def __init__(
        self,
        gpkg_path: Path,
        *,
        layer_name: str = "gis_osm_water_a_free",
    ) -> None:
        self.gpkg_path = gpkg_path
        self.layer_name = layer_name

    def extract(
        self,
        *,
        boundary_path: Path,
        output_path: Path,
    ) -> WaterExclusionResult:
        if not self.gpkg_path.exists():
            raise WaterExtractionError(
                f"GeoPackage not found: {self.gpkg_path}"
            )

        if not boundary_path.exists():
            raise WaterExtractionError(
                f"Boundary artifact not found: {boundary_path}"
            )

        boundary = gpd.read_file(boundary_path)

        if boundary.empty:
            raise WaterExtractionError("Boundary artifact is empty.")

        if boundary.crs is None:
            boundary = boundary.set_crs("EPSG:4326")

        boundary = boundary[
            boundary.geometry.notna()
            & ~boundary.geometry.is_empty
        ].copy()

        layer_sample = gpd.read_file(
            self.gpkg_path,
            layer=self.layer_name,
            rows=1,
        )

        if layer_sample.crs is None:
            raise WaterExtractionError(
                f"Water layer has no CRS: {self.layer_name}"
            )

        boundary_in_water_crs = boundary.to_crs(
            layer_sample.crs
        )

        water = gpd.read_file(
            self.gpkg_path,
            layer=self.layer_name,
            bbox=tuple(boundary_in_water_crs.total_bounds),
        )

        input_feature_count = len(water)

        if water.empty:
            raise WaterExtractionError(
                "No water polygons were found inside the boundary extent."
            )

        water = water[
            water.geometry.notna()
            & ~water.geometry.is_empty
        ].copy()

        water["geometry"] = water.geometry.make_valid()

        water = water.explode(
            index_parts=False,
            ignore_index=True,
        )

        water = water[
            water.geometry.geom_type.isin(
                ["Polygon", "MultiPolygon"]
            )
        ].copy()

        clipped = gpd.clip(
            water,
            boundary_in_water_crs,
            keep_geom_type=True,
        )

        clipped = clipped[
            clipped.geometry.notna()
            & ~clipped.geometry.is_empty
        ].copy()

        clipped["geometry"] = clipped.geometry.make_valid()

        clipped = clipped.explode(
            index_parts=False,
            ignore_index=True,
        )

        clipped = clipped[
            clipped.geometry.geom_type.isin(
                ["Polygon", "MultiPolygon"]
            )
        ].copy()

        if clipped.empty:
            raise WaterExtractionError(
                "Water clipping produced no polygon features."
            )

        metric_crs = boundary.estimate_utm_crs()

        if metric_crs is None:
            raise WaterExtractionError(
                "Unable to determine a projected CRS for area calculation."
            )

        total_area_km2 = float(
            clipped.to_crs(metric_crs).geometry.area.sum()
            / 1_000_000
        )

        clipped = clipped.to_crs("EPSG:4326")

        clipped["source_provider"] = (
            "Geofabrik / OpenStreetMap"
        )
        clipped["source_layer"] = self.layer_name
        clipped["source_license"] = "ODbL 1.0"
        clipped["source_attribution"] = (
            "© OpenStreetMap contributors"
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = output_path.with_name(
            output_path.stem + ".tmp.geojson"
        )

        clipped.to_file(
            temporary_path,
            driver="GeoJSON",
        )

        temporary_path.replace(output_path)

        return WaterExclusionResult(
            artifact_path=output_path,
            feature_count=len(clipped),
            total_area_km2=total_area_km2,
            input_feature_count=input_feature_count,
            source_layer=self.layer_name,
            metric_crs=metric_crs.to_string(),
        )

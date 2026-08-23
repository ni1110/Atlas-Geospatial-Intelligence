"""CRS-safe hard-constraint masking, raster processing, and vectorization."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform, unary_union

from renewable_site_agent.config import PATHS
from renewable_site_agent.schemas import ErrorCode, ToolResult


def _pyproj() -> tuple[Any, Any]:
    try:
        from pyproj import CRS, Transformer
    except ImportError as exc:
        raise RuntimeError("pyproj is required for metric GIS calculations") from exc
    return CRS, Transformer


def choose_local_crs(geometry: Any) -> str:
    centroid = geometry.centroid
    if centroid.y >= 84:
        return "EPSG:3413"
    if centroid.y <= -80:
        return "EPSG:3031"
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    return f"EPSG:{32600 + zone if centroid.y >= 0 else 32700 + zone}"


def project_geometry(geometry: Any, source_crs: str, target_crs: str) -> Any:
    _, Transformer = _pyproj()
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def clean_polygonal(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return GeometryCollection()
    valid = make_valid(geometry) if not geometry.is_valid else geometry
    if isinstance(valid, (Polygon, MultiPolygon)):
        return valid
    if isinstance(valid, GeometryCollection):
        polygons = [item for item in valid.geoms if isinstance(item, (Polygon, MultiPolygon))]
        return unary_union(polygons) if polygons else GeometryCollection()
    return GeometryCollection()


def geometry_from_geojson(value: dict[str, Any]) -> Any:
    if value.get("type") == "FeatureCollection":
        geometries = [shape(feature["geometry"]) for feature in value.get("features", []) if feature.get("geometry")]
        return clean_polygonal(unary_union(geometries)) if geometries else GeometryCollection()
    if value.get("type") == "Feature":
        return clean_polygonal(shape(value["geometry"]))
    return clean_polygonal(shape(value))


def feature_collection(geometries: Iterable[Any], properties: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    geoms = list(geometries)
    props = list(properties) if properties is not None else [{} for _ in geoms]
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": prop, "geometry": mapping(geom)}
            for geom, prop in zip(geoms, props, strict=True)
        ],
    }


def write_geojson(path: Path, value: dict[str, Any]) -> tuple[str, int]:
    destination = PATHS.assert_artifact_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    PATHS.guard_disk_space(len(data))
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return hashlib.sha256(data).hexdigest(), len(data)


def overpass_to_geometries(data: dict[str, Any]) -> list[Any]:
    geometries: list[Any] = []
    for element in data.get("elements", []):
        points = element.get("geometry") or []
        if element.get("type") == "node" and "lon" in element and "lat" in element:
            from shapely.geometry import Point

            geometries.append(Point(float(element["lon"]), float(element["lat"])))
        elif len(points) >= 2:
            coordinates = [(float(p["lon"]), float(p["lat"])) for p in points]
            if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
                try:
                    geometries.append(make_valid(Polygon(coordinates)))
                except Exception:
                    continue
            else:
                from shapely.geometry import LineString

                geometries.append(LineString(coordinates))
    return [geometry for geometry in geometries if not geometry.is_empty]


def _polygon_parts(geometry: Any) -> list[Polygon]:
    cleaned = clean_polygonal(geometry)
    if isinstance(cleaned, Polygon):
        return [cleaned]
    if isinstance(cleaned, MultiPolygon):
        return list(cleaned.geoms)
    return []


class GISExecutor:
    def apply_spatial_exclusion(
        self,
        aoi_geojson: dict[str, Any],
        exclusions_geojson: list[dict[str, Any]],
        raster_exclusions: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        try:
            suitable = geometry_from_geojson(aoi_geojson)
            for exclusion in exclusions_geojson:
                geometry = geometry_from_geojson(exclusion)
                if not geometry.is_empty:
                    suitable = clean_polygonal(suitable.difference(geometry))
            for specification in raster_exclusions or []:
                geometry = self._raster_exclusion_geometry(specification)
                if not geometry.is_empty:
                    suitable = clean_polygonal(suitable.difference(geometry))
            if suitable.is_empty:
                return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, "Hard constraints removed the entire AOI")
            return ToolResult.ok(
                artifact=feature_collection(_polygon_parts(suitable)),
                metadata={"geometry_valid": suitable.is_valid, "exclusion_count": len(exclusions_geojson) + len(raster_exclusions or [])},
            )
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def _raster_exclusion_geometry(self, specification: dict[str, Any]) -> Any:
        try:
            import rasterio
            from rasterio.features import shapes as raster_shapes
            from rasterio.warp import transform_geom
        except ImportError as exc:
            raise RuntimeError("rasterio is required for raster hard constraints") from exc
        path = specification["path"]
        mode = specification.get("mode", "gte")
        with rasterio.open(path) as source:
            data = source.read(1, masked=True)
            values = np.asarray(data)
            valid = ~np.ma.getmaskarray(data)
            if mode == "gte":
                excluded = valid & (values >= float(specification["threshold"]))
            elif mode == "gt":
                excluded = valid & (values > float(specification["threshold"]))
            elif mode == "in":
                excluded = valid & np.isin(values, specification.get("values", []))
            else:
                raise ValueError(f"Unsupported raster exclusion mode: {mode}")
            polygons = []
            for geom, value in raster_shapes(
                np.asarray(excluded, dtype=np.uint8), mask=excluded, transform=source.transform
            ):
                if int(value) != 1:
                    continue
                if str(source.crs) != "EPSG:4326":
                    geom = transform_geom(source.crs, "EPSG:4326", geom, precision=7)
                polygons.append(shape(geom))
        return clean_polygonal(unary_union(polygons)) if polygons else GeometryCollection()

    def build_candidate_polygons(
        self,
        suitable_geojson: dict[str, Any],
        minimum_area_km2: float = 0.01,
    ) -> ToolResult:
        try:
            suitable = geometry_from_geojson(suitable_geojson)
            local_crs = choose_local_crs(suitable)
            candidates = []
            properties = []
            for index, polygon in enumerate(_polygon_parts(suitable), 1):
                area = project_geometry(polygon, "EPSG:4326", local_crs).area / 1_000_000
                if area < minimum_area_km2:
                    continue
                candidate_id = f"candidate-{hashlib.sha256(polygon.wkb).hexdigest()[:12]}"
                candidates.append(polygon)
                properties.append(
                    {"candidate_id": candidate_id, "area_km2": area, "crs_for_area": local_crs}
                )
            candidates_and_props = sorted(
                zip(candidates, properties, strict=True),
                key=lambda item: (-item[1]["area_km2"], item[1]["candidate_id"]),
            )
            collection = feature_collection(
                [item[0] for item in candidates_and_props],
                [item[1] for item in candidates_and_props],
            )
            return ToolResult.ok(
                artifact=collection,
                metadata={"candidate_count": len(candidates_and_props), "sliver_threshold_km2": minimum_area_km2},
            )
        except RuntimeError as exc:
            return ToolResult.fail(ErrorCode.PROJECTION_ERROR, str(exc))
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def calculate_nearest_distance(
        self,
        candidates_geojson: dict[str, Any],
        target_geojson: dict[str, Any],
        property_name: str,
    ) -> ToolResult:
        try:
            features = candidates_geojson.get("features", [])
            targets = []
            if target_geojson.get("type") == "FeatureCollection":
                targets = [shape(f["geometry"]) for f in target_geojson.get("features", []) if f.get("geometry")]
            if not targets:
                overpass = target_geojson.get("overpass_json") or target_geojson
                targets = overpass_to_geometries(overpass)
            if not targets:
                return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, f"No target geometry for {property_name}")
            aoi_geometry = unary_union([shape(f["geometry"]) for f in features])
            local_crs = choose_local_crs(aoi_geometry)
            projected_target = unary_union(
                [project_geometry(target, "EPSG:4326", local_crs) for target in targets]
            )
            output = json.loads(json.dumps(candidates_geojson))
            for feature in output.get("features", []):
                candidate = project_geometry(shape(feature["geometry"]), "EPSG:4326", local_crs)
                feature.setdefault("properties", {})[property_name] = candidate.distance(projected_target) / 1000
            return ToolResult.ok(artifact=output, metadata={"distance_crs": local_crs, "units": "km"})
        except RuntimeError as exc:
            return ToolResult.fail(ErrorCode.PROJECTION_ERROR, str(exc))
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def calculate_candidate_statistics(
        self,
        candidates_geojson: dict[str, Any],
        slope_raster_path: str | None = None,
        land_cover_raster_path: str | None = None,
        land_cover_preferences: dict[str, float] | None = None,
        energy_resource_score: float | None = None,
    ) -> ToolResult:
        output = json.loads(json.dumps(candidates_geojson))
        try:
            import rasterio
            from rasterio.mask import mask as raster_mask
            from rasterio.warp import transform_geom
        except ImportError:
            rasterio = None
        try:
            slope_source = rasterio.open(slope_raster_path) if rasterio and slope_raster_path else None
            land_source = rasterio.open(land_cover_raster_path) if rasterio and land_cover_raster_path else None
            preferences = {int(key): float(value) for key, value in (land_cover_preferences or {}).items()}
            for feature in output.get("features", []):
                properties = feature.setdefault("properties", {})
                if slope_source:
                    geom = feature["geometry"]
                    if str(slope_source.crs) != "EPSG:4326":
                        geom = transform_geom("EPSG:4326", slope_source.crs, geom)
                    values, _ = raster_mask(slope_source, [geom], crop=True, filled=False)
                    band = values[0].compressed()
                    band = band[np.isfinite(band) & (band >= 0)]
                    if band.size:
                        properties["mean_slope_deg"] = float(np.mean(band))
                        properties["max_slope_deg_observed"] = float(np.max(band))
                if land_source:
                    geom = feature["geometry"]
                    if str(land_source.crs) != "EPSG:4326":
                        geom = transform_geom("EPSG:4326", land_source.crs, geom)
                    values, _ = raster_mask(land_source, [geom], crop=True, filled=False)
                    band = values[0].compressed().astype(int)
                    if band.size:
                        classes, counts = np.unique(band, return_counts=True)
                        distribution = {
                            str(int(code)): float(count / band.size)
                            for code, count in zip(classes, counts, strict=True)
                        }
                        properties["land_cover_distribution"] = distribution
                        if preferences:
                            properties["land_cover_score"] = float(
                                sum(distribution.get(str(code), 0) * score for code, score in preferences.items())
                            )
                if energy_resource_score is not None:
                    properties["energy_resource_score"] = float(energy_resource_score)
            if slope_source:
                slope_source.close()
            if land_source:
                land_source.close()
            return ToolResult.ok(artifact=output)
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def vectorize_raster_mask(
        self,
        raster_path: str,
        output_path: str,
        valid_values: list[int] | None = None,
        minimum_area_km2: float = 0.01,
    ) -> ToolResult:
        """Actually polygonize a raster mask; no manifest-only behavior."""
        try:
            import rasterio
            from rasterio.features import shapes as raster_shapes
            from rasterio.warp import transform_geom
        except ImportError as exc:
            return ToolResult.fail(ErrorCode.PROVIDER_UNAVAILABLE, f"rasterio unavailable: {exc}")
        try:
            with rasterio.open(raster_path) as source:
                data = source.read(1, masked=True)
                valid = ~np.ma.getmaskarray(data)
                if valid_values is not None:
                    valid &= np.isin(np.asarray(data), valid_values)
                features = []
                for geom, value in raster_shapes(np.asarray(valid, dtype=np.uint8), mask=valid, transform=source.transform):
                    if int(value) != 1:
                        continue
                    if str(source.crs) != "EPSG:4326":
                        geom = transform_geom(source.crs, "EPSG:4326", geom, precision=7)
                    features.append({"type": "Feature", "properties": {}, "geometry": geom})
            built = self.build_candidate_polygons(
                {"type": "FeatureCollection", "features": features}, minimum_area_km2
            )
            if built.status == "failed":
                return built
            checksum, size = write_geojson(Path(output_path), built.artifact)  # type: ignore[arg-type]
            built.metadata.update({"output_path": output_path, "checksum_sha256": checksum, "size_bytes": size})
            return built
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def calculate_slope(
        self,
        dem_path: str,
        output_path: str,
        max_slope_deg: float,
    ) -> ToolResult:
        try:
            import rasterio
        except ImportError as exc:
            return ToolResult.fail(ErrorCode.PROVIDER_UNAVAILABLE, f"rasterio unavailable: {exc}")
        try:
            with rasterio.open(dem_path) as source:
                dem = source.read(1, masked=True).astype("float64")
                if source.crs is None or source.crs.is_geographic:
                    return ToolResult.fail(
                        ErrorCode.PROJECTION_ERROR,
                        "DEM must be reprojected to a metric CRS before slope calculation",
                    )
                x_res, y_res = abs(source.transform.a), abs(source.transform.e)
                dz_dy, dz_dx = np.gradient(dem.filled(np.nan), y_res, x_res)
                slope = np.degrees(np.arctan(np.sqrt(dz_dx**2 + dz_dy**2))).astype("float32")
                profile = source.profile.copy()
                profile.update(dtype="float32", count=1, nodata=-9999, compress="deflate")
                destination = PATHS.assert_artifact_path(Path(output_path))
                destination.parent.mkdir(parents=True, exist_ok=True)
                PATHS.guard_disk_space(int(slope.nbytes * 1.2))
                with rasterio.open(destination, "w", **profile) as target:
                    target.write(np.where(np.isfinite(slope), slope, -9999), 1)
            return ToolResult.ok(
                artifact={"path": str(destination), "crs": str(profile.get("crs")), "max_slope_deg": max_slope_deg},
                metrics={"valid_fraction": float(np.isfinite(slope).mean())},
            )
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

    def validate_candidate_geometry(
        self,
        candidates_geojson: dict[str, Any],
        aoi_geojson: dict[str, Any],
        prohibited_geojson: list[dict[str, Any]] | None = None,
        minimum_area_km2: float | None = None,
    ) -> ToolResult:
        try:
            aoi = geometry_from_geojson(aoi_geojson)
            prohibited = [geometry_from_geojson(value) for value in (prohibited_geojson or [])]
            seen: set[str] = set()
            issues: list[dict[str, Any]] = []
            for feature in candidates_geojson.get("features", []):
                candidate_id = feature.get("properties", {}).get("candidate_id", "unknown")
                geometry = shape(feature["geometry"])
                geometry_hash = hashlib.sha256(geometry.wkb).hexdigest()
                checks = {
                    "valid": geometry.is_valid,
                    "non_empty": not geometry.is_empty,
                    "inside_aoi": aoi.covers(geometry),
                    "no_prohibited_overlap": not any(geometry.intersection(item).area > 1e-12 for item in prohibited),
                    "not_duplicate": geometry_hash not in seen,
                }
                seen.add(geometry_hash)
                if minimum_area_km2 is not None:
                    local_crs = choose_local_crs(geometry)
                    checks["minimum_area"] = project_geometry(geometry, "EPSG:4326", local_crs).area / 1_000_000 >= minimum_area_km2
                if not all(checks.values()):
                    issues.append({"candidate_id": candidate_id, "checks": checks})
            if issues:
                return ToolResult.fail(
                    ErrorCode.INVALID_OUTPUT, "Candidate validation failed",
                    metadata={"issues": issues, "candidate_count": len(candidates_geojson.get("features", []))},
                )
            return ToolResult.ok(metadata={"validation_passed": True, "candidate_count": len(candidates_geojson.get("features", []))})
        except Exception as exc:
            return ToolResult.fail(ErrorCode.GEOMETRY_ERROR, f"{type(exc).__name__}: {exc}")

"""Local administrative-boundary provider backed by geoBoundaries."""

import json
from pathlib import Path
from typing import Iterator

from renewable_site_agent.adapters.admin_boundary import (
    BoundaryResolutionError,
    BoundaryResult,
)


REGION_ALIASES = {
    "北京": "Beijing Municipality",
    "北京市": "Beijing Municipality",
}


def _iter_coordinates(value: object) -> Iterator[tuple[float, float]]:
    """Yield all longitude/latitude pairs from GeoJSON coordinates."""
    if not isinstance(value, list):
        return

    if (
        len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
        return

    for item in value:
        yield from _iter_coordinates(item)


class LocalGeoBoundariesProvider:
    """Resolve Chinese ADM1 boundaries from a local geoBoundaries file."""

    def __init__(self, dataset_path: Path) -> None:
        self.dataset_path = dataset_path

    def resolve(self, region: str) -> BoundaryResult:
        if not self.dataset_path.exists():
            raise BoundaryResolutionError(
                f"Local boundary dataset not found: {self.dataset_path}"
            )

        try:
            payload = json.loads(
                self.dataset_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise BoundaryResolutionError(
                f"Unable to read local boundary dataset: {exc}"
            ) from exc

        features = payload.get("features")
        if not isinstance(features, list):
            raise BoundaryResolutionError(
                "Local boundary dataset is not a valid FeatureCollection."
            )

        normalized_region = region.strip()
        expected_name = REGION_ALIASES.get(
            normalized_region,
            normalized_region,
        )

        selected = next(
            (
                feature
                for feature in features
                if str(
                    feature.get("properties", {}).get("shapeName", "")
                ).casefold()
                == expected_name.casefold()
            ),
            None,
        )

        if selected is None:
            raise BoundaryResolutionError(
                f"No local administrative boundary found for: {region}"
            )

        geometry = selected.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise BoundaryResolutionError(
                f"Invalid boundary geometry for: {region}"
            )

        positions = list(
            _iter_coordinates(geometry.get("coordinates"))
        )
        if not positions:
            raise BoundaryResolutionError(
                f"Boundary contains no coordinates: {region}"
            )

        longitudes = [position[0] for position in positions]
        latitudes = [position[1] for position in positions]

        properties = selected.get("properties", {})
        shape_name = str(properties.get("shapeName", expected_name))
        shape_id = str(properties.get("shapeID", "unknown"))

        return BoundaryResult(
            geometry=geometry,
            display_name=shape_name,
            source_identifier=f"geoBoundaries:{shape_id}",
            source_license="CC BY 4.0",
            bounding_box=[
                str(min(latitudes)),
                str(max(latitudes)),
                str(min(longitudes)),
                str(max(longitudes)),
            ],
            metadata={
                "provider": "geoBoundaries",
                "dataset_path": str(self.dataset_path),
                "shape_name": shape_name,
                "shape_id": shape_id,
                "shape_iso": properties.get("shapeISO"),
                "shape_type": properties.get("shapeType"),
            },
        )

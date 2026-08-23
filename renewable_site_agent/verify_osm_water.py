"""Direct verification for the OSM water extraction adapter."""

from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
from shapely.geometry import Polygon

from renewable_site_agent.adapters.osm_water import (
    GeofabrikWaterProvider,
)


def main() -> None:
    runtime_tmp = Path("runtime/tmp")
    runtime_tmp.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(
        dir=runtime_tmp,
        prefix="verify_water_",
    ) as temporary_directory:
        temp_path = Path(temporary_directory)

        boundary_path = temp_path / "boundary.geojson"
        gpkg_path = temp_path / "water.gpkg"
        output_path = temp_path / "water_exclusion.geojson"

        boundary = gpd.GeoDataFrame(
            {"name": ["test-boundary"]},
            geometry=[
                Polygon(
                    [
                        (116.0, 39.5),
                        (117.0, 39.5),
                        (117.0, 40.5),
                        (116.0, 40.5),
                        (116.0, 39.5),
                    ]
                )
            ],
            crs="EPSG:4326",
        )

        boundary.to_file(
            boundary_path,
            driver="GeoJSON",
        )

        water = gpd.GeoDataFrame(
            {
                "osm_id": [1, 2],
                "code": [8200, 8200],
                "fclass": ["water", "water"],
                "name": ["inside", "outside"],
            },
            geometry=[
                Polygon(
                    [
                        (116.2, 39.7),
                        (116.4, 39.7),
                        (116.4, 39.9),
                        (116.2, 39.9),
                        (116.2, 39.7),
                    ]
                ),
                Polygon(
                    [
                        (118.0, 41.0),
                        (118.2, 41.0),
                        (118.2, 41.2),
                        (118.0, 41.2),
                        (118.0, 41.0),
                    ]
                ),
            ],
            crs="EPSG:4326",
        )

        water.to_file(
            gpkg_path,
            layer="gis_osm_water_a_free",
            driver="GPKG",
        )

        result = GeofabrikWaterProvider(
            gpkg_path
        ).extract(
            boundary_path=boundary_path,
            output_path=output_path,
        )

        extracted = gpd.read_file(output_path)

        assert result.feature_count == 1
        assert result.total_area_km2 > 0
        assert extracted.iloc[0]["name"] == "inside"
        assert output_path.exists()

        print("PASS：水体裁剪逻辑验证成功")
        print("输出水体数量：", result.feature_count)
        print("水体总面积 km²：", round(result.total_area_km2, 6))


if __name__ == "__main__":
    main()

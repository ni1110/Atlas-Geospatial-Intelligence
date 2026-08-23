"""Real remote-first providers. Providers perform one attempt; reliability is external."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from renewable_site_agent.config import PATHS, load_config
from renewable_site_agent.data.cache import sha256_bytes
from renewable_site_agent.schemas import DataArtifact, ErrorCode, ToolResult

NETWORK = load_config("data_sources.yaml").get("network", {})
USER_AGENT = str(NETWORK.get("user_agent", "GeoAgent-RenewableSite/2.0"))
DEFAULT_TIMEOUT = float(NETWORK.get("timeout_seconds", 30))
MAX_MATERIALIZED_PIXELS = int(NETWORK.get("max_materialized_pixels", 50_000_000))


class Provider(Protocol):
    name: str
    dataset: str
    version: str

    def fetch(self, query: dict[str, Any]) -> ToolResult: ...


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Any, int, float]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/geo+json",
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    return json.loads(data), len(data), (time.perf_counter() - started) * 1000


def _artifact(
    *,
    provider: str,
    dataset: str,
    version: str,
    source_url: str,
    query: dict[str, Any],
    license_name: str,
    attribution: str,
    crs: str = "EPSG:4326",
    resolution: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DataArtifact:
    return DataArtifact(
        provider=provider,
        dataset=dataset,
        dataset_version=version,
        source_url=source_url,
        aoi=query.get("aoi"),
        bbox=query.get("bbox"),
        crs=crs,
        resolution=resolution,
        license=license_name,
        attribution=attribution,
        metadata=metadata or {},
    )


def materialize_remote_raster(
    *,
    urls: list[str],
    bbox: tuple[float, float, float, float],
    dataset: str,
    key: str,
    target_crs: str | None = None,
    categorical: bool = False,
) -> tuple[Path, str]:
    """Read only an AOI window from remote COGs and persist a clipped raster."""
    try:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.merge import merge
        from rasterio.warp import calculate_default_transform, reproject
    except ImportError as exc:
        raise RuntimeError("rasterio is required to materialize remote raster windows") from exc
    output_dir = PATHS.cache / "raster" / dataset
    output_dir.mkdir(parents=True, exist_ok=True)
    output = PATHS.assert_artifact_path(output_dir / f"{key}.tif")
    if output.is_file():
        with rasterio.open(output) as cached:
            return output, str(cached.crs)
    sources = []
    temporary = output.with_suffix(".source.tmp.tif")
    try:
        for url in urls:
            sources.append(rasterio.open(url))
        if not sources:
            raise FileNotFoundError("No raster source URLs")
        first = sources[0]
        if first.crs is None:
            raise ValueError("Raster source has no CRS")
        source_bbox = bbox
        if not first.crs.is_geographic:
            from rasterio.warp import transform_bounds

            source_bbox = transform_bounds("EPSG:4326", first.crs, *bbox, densify_pts=21)
        x_res, y_res = abs(first.transform.a), abs(first.transform.e)
        predicted = max(1, int((source_bbox[2] - source_bbox[0]) / x_res)) * max(
            1, int((source_bbox[3] - source_bbox[1]) / y_res)
        )
        if predicted > MAX_MATERIALIZED_PIXELS:
            raise RuntimeError(
                f"AOI raster window would contain about {predicted:,} pixels; "
                f"limit is {MAX_MATERIALIZED_PIXELS:,}. Supply a smaller AOI."
            )
        import numpy as np

        bytes_per_pixel = first.count * max(4, np.dtype(first.dtypes[0]).itemsize)
        PATHS.guard_disk_space(predicted * bytes_per_pixel)
        merge_nodata = first.nodata
        if merge_nodata is None and np.issubdtype(np.dtype(first.dtypes[0]), np.floating):
            # Rasterio merge otherwise initializes uncovered edge pixels to 0,
            # which is a valid elevation and creates false cliffs after warp.
            merge_nodata = -9999.0
        mosaic, transform_value = merge(sources, bounds=source_bbox, nodata=merge_nodata)
        profile = first.profile.copy()
        profile.update(
            driver="GTiff", height=mosaic.shape[1], width=mosaic.shape[2],
            transform=transform_value, count=mosaic.shape[0], compress="deflate",
            nodata=merge_nodata,
        )
        with rasterio.open(temporary, "w", **profile) as target:
            target.write(mosaic)
        if target_crs and str(first.crs) != target_crs:
            with rasterio.open(temporary) as source:
                transform_out, width, height = calculate_default_transform(
                    source.crs, target_crs, source.width, source.height, *source.bounds
                )
                destination_nodata = source.nodata
                if destination_nodata is None:
                    destination_nodata = -9999.0
                output_profile = source.profile.copy()
                output_profile.update(
                    crs=target_crs,
                    transform=transform_out,
                    width=width,
                    height=height,
                    nodata=destination_nodata,
                )
                with rasterio.open(output, "w", **output_profile) as target:
                    for band in range(1, source.count + 1):
                        reproject(
                            source=rasterio.band(source, band), destination=rasterio.band(target, band),
                            src_transform=source.transform, src_crs=source.crs,
                            dst_transform=transform_out, dst_crs=target_crs,
                            src_nodata=source.nodata, dst_nodata=destination_nodata,
                            init_dest_nodata=True,
                            resampling=Resampling.nearest if categorical else Resampling.bilinear,
                        )
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(output)
        with rasterio.open(output) as materialized:
            output_crs = str(materialized.crs)
        return output, output_crs
    finally:
        for source in sources:
            source.close()
        temporary.unlink(missing_ok=True)


PROVINCE_ENGLISH = {
    "北京": "Beijing", "北京市": "Beijing", "甘肃": "Gansu", "甘肃省": "Gansu",
    "四川": "Sichuan", "四川省": "Sichuan", "广东": "Guangdong", "广东省": "Guangdong",
    "上海": "Shanghai", "天津": "Tianjin", "重庆": "Chongqing", "河北": "Hebei",
    "河南": "Henan", "山东": "Shandong", "山西": "Shanxi", "陕西": "Shaanxi",
    "青海": "Qinghai", "云南": "Yunnan", "贵州": "Guizhou", "湖南": "Hunan",
    "湖北": "Hubei", "江西": "Jiangxi", "福建": "Fujian", "浙江": "Zhejiang",
    "江苏": "Jiangsu", "安徽": "Anhui", "辽宁": "Liaoning", "吉林": "Jilin",
    "黑龙江": "Heilongjiang", "海南": "Hainan", "内蒙古": "Inner Mongolia",
    "广西": "Guangxi", "西藏": "Tibet", "宁夏": "Ningxia", "新疆": "Xinjiang",
}

# Keep critical Chinese aliases as Unicode escapes.  Some historical source
# files passed through a legacy code page, so these entries must not depend on
# the editor or console encoding.
PROVINCE_ENGLISH.update(
    {
        "\u7518\u8083": "Gansu",
        "\u7518\u8083\u7701": "Gansu",
    }
)


def _normalized_region(region: str) -> str:
    value = PROVINCE_ENGLISH.get(region.strip(), region.strip())
    return re.sub(r"\s+(Province|Municipality|Autonomous Region)$", "", value, flags=re.IGNORECASE).lower()


def _select_boundary(collection: dict[str, Any], region: str) -> dict[str, Any] | None:
    target = _normalized_region(region)
    for feature in collection.get("features", []):
        properties = feature.get("properties") or {}
        names = [
            properties.get(key, "")
            for key in ("shapeName", "name", "NAME_1", "VARNAME_1", "NL_NAME_1")
        ]
        normalized = [_normalized_region(str(name)) for name in names if name]
        if any(target == name or target in name or name in target for name in normalized):
            return feature
    return None


@dataclass
class GeoBoundariesProvider:
    name: str = "geoboundaries"
    dataset: str = "administrative_boundary"
    version: str = "current"
    api_url: str = "https://www.geoboundaries.org/api/current/gbOpen/CHN/ADM1/"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        region = str(query.get("region") or "").strip()
        if not region:
            return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "region is required")
        metadata, metadata_bytes, latency = http_json(self.api_url)
        download_url = metadata.get("gjDownloadURL") or metadata.get("simplifiedGeometryGeoJSON")
        if not download_url:
            return ToolResult.fail(ErrorCode.INVALID_OUTPUT, "geoBoundaries metadata lacks GeoJSON URL")
        collection, data_bytes, download_latency = http_json(download_url)
        feature = _select_boundary(collection, region)
        if feature is None:
            return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, f"Region not found in CHN ADM1: {region}")
        normalized = {"type": "FeatureCollection", "features": [feature]}
        raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=str(metadata.get("boundaryYear", self.version)),
            source_url=download_url, query=query, license_name="CC BY 4.0",
            attribution="geoBoundaries", metadata={"feature_collection": normalized, "boundary_type": "ADM1"},
        )
        artifact.checksum_sha256 = sha256_bytes(raw)
        return ToolResult.ok(
            artifact=artifact, provenance=[artifact],
            metrics={"bytes_downloaded": metadata_bytes + data_bytes, "download_latency_ms": latency + download_latency, "cache_hit": False},
        )


@dataclass
class LocalGeoBoundariesProvider:
    name: str = "local_geoboundaries"
    dataset: str = "administrative_boundary"
    version: str = "cached_regression"
    path: Path = PATHS.project_root / "runtime" / "reference" / "boundaries" / "geoBoundaries-CHN-ADM1.geojson"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        region = str(query.get("region") or "").strip()
        if not self.path.is_file():
            return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, f"Local boundary cache missing: {self.path}")
        collection = json.loads(self.path.read_text(encoding="utf-8"))
        feature = _select_boundary(collection, region)
        if feature is None:
            return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, f"Region not in local ADM1 cache: {region}")
        normalized = {"type": "FeatureCollection", "features": [feature]}
        raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url=str(self.path), query=query, license_name="CC BY 4.0",
            attribution="geoBoundaries", metadata={"feature_collection": normalized, "boundary_type": "ADM1"},
        )
        artifact.local_path = str(self.path)
        artifact.checksum_sha256 = sha256_bytes(raw)
        artifact.source_degraded = True
        artifact.quality_flag = "degraded"
        return ToolResult(
            status="degraded", degraded=True, message="Using valid local geoBoundaries cache",
            artifact=artifact, provenance=[artifact], original_provider="geoboundaries",
            fallback_provider=self.name, fallback_reason="remote provider unavailable",
            metrics={"bytes_downloaded": 0, "download_latency_ms": 0, "cache_hit": True},
        )


def _bbox(query: dict[str, Any]) -> tuple[float, float, float, float]:
    bbox = query.get("bbox")
    if not bbox or len(bbox) != 4:
        raise ValueError("bbox W,S,E,N is required")
    west, south, east, north = map(float, bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("invalid bbox")
    return west, south, east, north


def _copernicus_tile(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    stem = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
    return f"https://copernicus-dem-30m.s3.amazonaws.com/{stem}/{stem}.tif"


@dataclass
class CopernicusDEMProvider:
    name: str = "copernicus_glo30"
    dataset: str = "dem"
    version: str = "2023_1"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        west, south, east, north = _bbox(query)
        urls = [
            _copernicus_tile(lat, lon)
            for lat in range(math.floor(south), math.ceil(north))
            for lon in range(math.floor(west), math.ceil(east))
        ]
        local_path = None
        raster_crs = "EPSG:4326"
        if query.get("materialize"):
            centroid_lon, centroid_lat = (west + east) / 2, (south + north) / 2
            zone = max(1, min(60, int((centroid_lon + 180) // 6) + 1))
            target_crs = f"EPSG:{32600 + zone if centroid_lat >= 0 else 32700 + zone}"
            key = sha256_bytes(json.dumps({
                "urls": urls,
                "bbox": [west, south, east, north],
                "normalization": "metric-nodata-v2",
            }).encode())[:24]
            local_path, raster_crs = materialize_remote_raster(
                urls=urls, bbox=(west, south, east, north), dataset=self.dataset,
                key=key, target_crs=target_crs,
            )
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url="https://copernicus-dem-30m.s3.amazonaws.com/", query=query,
            license_name="Copernicus DEM License", attribution="Copernicus DEM GLO-30",
            crs=raster_crs, resolution="30 m", metadata={"remote_cog_urls": urls, "access_mode": "remote_window_or_tile"},
        )
        if local_path:
            artifact.local_path = str(local_path)
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"cache_hit": False, "tile_count": len(urls), "bytes_downloaded": 0})


def _worldcover_tile(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    stem = f"ESA_WorldCover_10m_2021_v200_{ns}{abs(lat):02d}{ew}{abs(lon):03d}_Map"
    return f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{stem}.tif"


@dataclass
class WorldCoverProvider:
    name: str = "esa_worldcover"
    dataset: str = "land_cover"
    version: str = "2021_v200"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        west, south, east, north = _bbox(query)
        lon0, lat0 = math.floor(west / 3) * 3, math.floor(south / 3) * 3
        urls = []
        for lat in range(lat0, math.ceil(north / 3) * 3, 3):
            for lon in range(lon0, math.ceil(east / 3) * 3, 3):
                urls.append(_worldcover_tile(lat, lon))
        local_path = None
        raster_crs = "EPSG:4326"
        if query.get("materialize"):
            key = sha256_bytes(json.dumps({"urls": urls, "bbox": [west, south, east, north]}).encode())[:24]
            local_path, raster_crs = materialize_remote_raster(
                urls=urls, bbox=(west, south, east, north), dataset=self.dataset,
                key=key, categorical=True,
            )
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url="https://esa-worldcover.org/", query=query,
            license_name="CC BY 4.0", attribution="ESA WorldCover project 2021",
            crs=raster_crs, resolution="10 m", metadata={"remote_cog_urls": urls, "access_mode": "remote_window"},
        )
        if local_path:
            artifact.local_path = str(local_path)
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"tile_count": len(urls), "bytes_downloaded": 0, "cache_hit": False})


@dataclass
class JRCSurfaceWaterProvider:
    name: str = "jrc_gsw"
    dataset: str = "water"
    version: str = "1984-2024"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        west, south, east, north = _bbox(query)
        tiles = []
        for lon in range(math.floor(west / 10) * 10, math.ceil(east / 10) * 10, 10):
            for lat in range(math.ceil(north / 10) * 10, math.floor(south / 10) * 10, -10):
                ew = f"{abs(lon)}{'E' if lon >= 0 else 'W'}"
                ns = f"{abs(lat)}{'N' if lat >= 0 else 'S'}"
                name = f"occurrence_{ew}_{ns}v1_4_2021.tif"
                tiles.append(f"https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/{name}")
        local_path = None
        raster_crs = "EPSG:4326"
        if query.get("materialize"):
            key = sha256_bytes(json.dumps({"urls": tiles, "bbox": [west, south, east, north]}).encode())[:24]
            local_path, raster_crs = materialize_remote_raster(
                urls=tiles, bbox=(west, south, east, north), dataset=self.dataset,
                key=key, categorical=True,
            )
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url="https://global-surface-water.appspot.com/", query=query,
            license_name="Copernicus free and open data", attribution="EC JRC Global Surface Water",
            crs=raster_crs, resolution="30 m", metadata={"remote_raster_urls": tiles, "band": "occurrence", "hard_exclusion_threshold": query.get("occurrence_threshold", 50)},
        )
        if local_path:
            artifact.local_path = str(local_path)
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"tile_count": len(tiles), "bytes_downloaded": 0, "cache_hit": False})


OVERPASS_URL = "https://overpass-api.de/api/interpreter"


@dataclass
class OSMOverpassProvider:
    dataset: str
    name: str = "osm_overpass"
    version: str = "live"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        west, south, east, north = _bbox(query)
        box = f"{south},{west},{north},{east}"
        selectors = {
            "water": '["natural"="water"]',
            "protected_areas": '["boundary"="protected_area"]',
            "power_infrastructure": '["power"="substation"]',
            "roads": '["highway"~"motorway|trunk|primary|secondary|tertiary"]',
        }
        selector = selectors.get(self.dataset)
        if selector is None:
            return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, f"Unsupported OSM dataset: {self.dataset}")
        overpass_query = f"[out:json][timeout:25];(node{selector}({box});way{selector}({box});relation{selector}({box}););out geom;"
        url = f"{OVERPASS_URL}?{urllib.parse.urlencode({'data': overpass_query})}"
        data, size, latency = http_json(url)
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url=OVERPASS_URL, query=query, license_name="ODbL 1.0",
            attribution="© OpenStreetMap contributors",
            metadata={"overpass_json": data, "proxy_notice": "OSM proxy; not an official legal or grid-completeness source"},
        )
        if self.dataset == "protected_areas":
            artifact.metadata["protected_area_source"] = "OSM_PROXY"
        if self.dataset == "power_infrastructure":
            artifact.metadata["infrastructure_source"] = "OSM_PROXY"
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"bytes_downloaded": size, "download_latency_ms": latency, "cache_hit": False})


@dataclass
class NASAPowerProvider:
    name: str = "nasa_power"
    dataset: str = "energy_resource"
    version: str = "daily_v2.8.1"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        west, south, east, north = _bbox(query)
        lat, lon = (south + north) / 2, (west + east) / 2
        project = query.get("project_type", "solar")
        parameters = "ALLSKY_SFC_SW_DWN" if project == "solar" else "WS50M"
        endpoint = "https://power.larc.nasa.gov/api/temporal/climatology/point"
        url = endpoint + "?" + urllib.parse.urlencode(
            {"parameters": parameters, "community": "RE", "longitude": lon, "latitude": lat, "format": "JSON"}
        )
        data, size, latency = http_json(url)
        parameter_data = data.get("properties", {}).get("parameter", {}).get(parameters, {})
        if not parameter_data:
            return ToolResult.fail(ErrorCode.INVALID_OUTPUT, f"NASA POWER missing {parameters}")
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url=url, query=query, license_name="NASA open data",
            attribution="NASA POWER", resolution="point climatology (~0.5°)",
            metadata={"parameter": parameters, "values": parameter_data, "units": data.get("parameters", {}).get(parameters, {}).get("units")},
        )
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"bytes_downloaded": size, "download_latency_ms": latency, "cache_hit": False})


@dataclass
class SentinelSTACProvider:
    name: str = "copernicus_dataspace_stac"
    dataset: str = "sentinel"
    version: str = "Sentinel-2-L2A"
    endpoint: str = "https://stac.dataspace.copernicus.eu/v1/search"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        bbox = list(_bbox(query))
        date_range = str(query.get("date_range", "2025-01-01/2026-12-31"))
        start, separator, end = date_range.partition("/")
        if separator and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            start += "T00:00:00Z"
        if separator and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            end += "T23:59:59Z"
        normalized_date_range = f"{start}/{end}" if separator else date_range
        payload = {
            "collections": ["sentinel-2-l2a"], "bbox": bbox, "limit": int(query.get("limit", 10)),
            "datetime": normalized_date_range,
            "query": {"eo:cloud_cover": {"lte": float(query.get("cloud_cover", 20))}},
        }
        data, size, latency = http_json(self.endpoint, method="POST", payload=payload)
        features = data.get("features", [])
        if not features:
            return ToolResult.fail(ErrorCode.DATA_NOT_FOUND, "No matching Sentinel-2 L2A item")
        artifact = _artifact(
            provider=self.name, dataset=self.dataset, version=self.version,
            source_url=self.endpoint, query=query, license_name="Copernicus Sentinel Data Terms",
            attribution="Copernicus Sentinel-2", resolution="10/20/60 m by band",
            metadata={"items": features, "current_job_search": True},
        )
        return ToolResult.ok(artifact=artifact, provenance=[artifact], metrics={"item_count": len(features), "bytes_downloaded": size, "download_latency_ms": latency, "cache_hit": False})


@dataclass
class PlanetaryComputerSTACProvider(SentinelSTACProvider):
    name: str = "planetary_computer_stac"
    endpoint: str = "https://planetarycomputer.microsoft.com/api/stac/v1/search"

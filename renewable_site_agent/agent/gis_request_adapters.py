"""Real GIS adapters for request-driven renewable-site jobs.

Engineering notes:
- Vector layers are always read by GeoPandas and re-written as real GeoJSON.
  We never copy Shapefile bytes into a .geojson path.
- Dynamic slope screening supports arbitrary numeric thresholds.
- To avoid full Beijing mask polygonization, slope screening uses candidate-level
  raster statistics with bounded sampling.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask as raster_mask
from shapely.geometry import mapping


def _project_root() -> Path:
    return Path.cwd()


def _clean_gdf(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    if "geometry" not in gdf.columns:
        raise ValueError("Input vector has no geometry column.")
    gdf = gdf[gdf.geometry.notnull()]
    gdf = gdf[~gdf.geometry.is_empty]
    if len(gdf) == 0:
        return gdf
    gdf["geometry"] = gdf.geometry.buffer(0)
    gdf = gdf[gdf.geometry.notnull()]
    gdf = gdf[~gdf.geometry.is_empty]
    return gdf


def _read_vector(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    return _clean_gdf(gdf)


def _write_vector(gdf: gpd.GeoDataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    gdf = _clean_gdf(gdf)
    gdf.to_file(path, driver="GeoJSON")


def _count_features(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return len(gpd.read_file(path))
    except Exception:
        return None


def _candidate_files(roots: list[Path], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(
                p
                for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in suffixes
            )
    return sorted(set(files), key=lambda p: len(str(p)))



def _is_readable_vector_file(path: Path) -> bool:
    """Return True only for real readable vector layers.

    This prevents metadata JSON files from being selected as GeoJSON layers.
    """
    text = str(path).lower()
    blocked_names = (
        "metadata",
        "manifest",
        "summary",
        "validation",
        "execution",
        "run_v",
    )
    if any(name in text for name in blocked_names):
        return False

    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if obj.get("type") not in {"FeatureCollection", "Feature"}:
            return False

    try:
        gdf = gpd.read_file(path)
    except Exception:
        return False

    return "geometry" in gdf.columns



def _find_file(
    roots: list[Path],
    suffixes: tuple[str, ...],
    must_contain_any: tuple[str, ...],
    prefer_any: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
    require_readable_vector: bool = False,
) -> Path | None:
    scored: list[tuple[int, Path]] = []

    for path in _candidate_files(roots, suffixes):
        text = str(path).lower()

        if exclude_any and any(x.lower() in text for x in exclude_any):
            continue

        if must_contain_any and not any(x.lower() in text for x in must_contain_any):
            continue

        if require_readable_vector and not _is_readable_vector_file(path):
            continue

        score = 0
        for keyword in prefer_any:
            if keyword.lower() in text:
                score += 10

        if "runtime/jobs" in text:
            score += 2

        scored.append((score, path))

    if not scored:
        return None

    scored.sort(key=lambda item: (-item[0], len(str(item[1]))))
    return scored[0][1]


def find_administrative_boundary(baseline_dir: Path) -> Path | None:
    roots = [baseline_dir, _project_root()]
    return _find_file(
        roots=roots,
        suffixes=(".geojson", ".json", ".shp"),
        must_contain_any=("boundary", "admin", "administrative", "beijing"),
        prefer_any=("beijing", "boundary", "admin"),
        exclude_any=("request-job", "request_scoped", "candidate_sites_after", "metadata", "manifest", "summary"),
        require_readable_vector=True,
    )


def find_protected_area_layer(baseline_dir: Path) -> Path | None:
    roots = [baseline_dir, _project_root()]
    return _find_file(
        roots=roots,
        suffixes=(".geojson", ".json", ".shp"),
        must_contain_any=("protected", "reserve", "protection", "nature"),
        prefer_any=("protected", "areas", "reserve", "union"),
        exclude_any=(
            "request-job",
            "request_scoped",
            "candidate_sites",
            "available_data_screened_candidate",
            "metadata",
            "manifest",
            "summary",
        ),
        require_readable_vector=True,
    )


def find_dem_raster(baseline_dir: Path) -> Path | None:
    roots = [baseline_dir, _project_root()]
    return _find_file(
        roots=roots,
        suffixes=(".tif", ".tiff"),
        must_contain_any=("dem", "elevation"),
        prefer_any=("beijing", "utm50", "dem"),
        exclude_any=("slope", "mask", "request-job"),
    )


def materialize_administrative_boundary(
    baseline_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    src = find_administrative_boundary(baseline_dir)
    if src is None:
        raise FileNotFoundError(
            "Cannot find administrative boundary vector. Expected filename containing boundary/admin/beijing."
        )

    gdf = _read_vector(src)
    _write_vector(gdf, output_path)

    return {
        "artifact_path": str(output_path),
        "source_path": str(src),
        "candidate_count": len(gdf),
    }


def prepare_protected_area_layer(
    baseline_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    src = find_protected_area_layer(baseline_dir)
    if src is None:
        raise FileNotFoundError(
            "Cannot find protected-area layer. Expected filename containing protected/reserve/nature."
        )

    gdf = _read_vector(src)
    _write_vector(gdf, output_path)

    return {
        "artifact_path": str(output_path),
        "source_path": str(src),
        "feature_count": len(gdf),
    }


def apply_protected_area_screening(
    candidate_path: Path,
    protected_area_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    candidates = _read_vector(candidate_path)
    protected = _read_vector(protected_area_path)

    if candidates.crs is None and protected.crs is not None:
        candidates = candidates.set_crs(protected.crs, allow_override=True)

    if protected.crs is None and candidates.crs is not None:
        protected = protected.set_crs(candidates.crs, allow_override=True)

    if candidates.crs is not None and protected.crs is not None and candidates.crs != protected.crs:
        protected = protected.to_crs(candidates.crs)

    protected_union = protected.geometry.unary_union

    result = candidates.copy()
    result["geometry"] = result.geometry.difference(protected_union)
    result = _clean_gdf(result)

    _write_vector(result, output_path)

    return {
        "artifact_path": str(output_path),
        "candidate_count": len(result),
        "input_candidate_count": len(candidates),
        "protected_area_feature_count": len(protected),
        "screening_method": "geometry_difference",
    }


def prepare_dem(
    baseline_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    src = find_dem_raster(baseline_dir)
    if src is None:
        raise FileNotFoundError(
            "Cannot find DEM raster. Expected filename containing dem/elevation."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists():
        shutil.copy2(src, output_path)

    return {
        "artifact_path": str(output_path),
        "source_path": str(src),
    }


def calculate_slope_raster(
    dem_path: Path,
    output_slope_path: Path,
) -> dict[str, Any]:
    output_slope_path.parent.mkdir(parents=True, exist_ok=True)

    if output_slope_path.is_file():
        return {
            "artifact_path": str(output_slope_path),
            "slope_unit": "degree",
            "cache_hit": True,
        }

    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype("float64")
        nodata = src.nodata

        invalid = np.zeros(dem.shape, dtype=bool)
        if nodata is not None:
            invalid |= dem == nodata
        invalid |= ~np.isfinite(dem)

        dem[invalid] = np.nan

        x_res = abs(src.transform.a)
        y_res = abs(src.transform.e)

        grad_y, grad_x = np.gradient(dem, y_res, x_res)
        slope_rad = np.arctan(np.sqrt(grad_x * grad_x + grad_y * grad_y))
        slope_deg = np.degrees(slope_rad)
        slope_deg[invalid] = np.nan

        profile = src.profile.copy()
        profile.update(
            dtype=rasterio.float32,
            count=1,
            nodata=-9999.0,
            compress="deflate",
        )

        output = np.where(np.isfinite(slope_deg), slope_deg, -9999.0).astype("float32")

        with rasterio.open(output_slope_path, "w", **profile) as dst:
            dst.write(output, 1)

    return {
        "artifact_path": str(output_slope_path),
        "slope_unit": "degree",
        "cache_hit": False,
    }


def vectorize_slope_mask(
    slope_raster_path: Path,
    max_slope_deg: float,
    output_mask_path: Path,
) -> dict[str, Any]:
    """Create a slope-screening manifest.

    This intentionally does not polygonize the whole Beijing slope mask.
    Full-mask polygonization is slow and unnecessary for request-time execution.
    """
    if not isinstance(max_slope_deg, (int, float)):
        raise ValueError("max_slope_deg must be numeric.")

    threshold = float(max_slope_deg)
    if not (0 < threshold < 90):
        raise ValueError("max_slope_deg must satisfy 0 < threshold < 90.")

    output_mask_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "slope_mask_type": "candidate_level_raster_statistics",
        "slope_raster_path": str(slope_raster_path),
        "max_slope_deg": threshold,
        "allowed_ratio_threshold": 0.50,
        "max_pixels_per_candidate": 5000,
        "note_zh": "该动态坡度筛选使用候选区内坡度栅格采样统计，避免整张北京坡度掩膜矢量化卡死。",
    }

    output_mask_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "artifact_path": str(output_mask_path),
        "max_slope_deg": threshold,
        "mask_polygon_count": None,
        "screening_method": "candidate_level_raster_statistics_manifest",
    }


def _sample_values(values: np.ndarray, max_pixels: int) -> np.ndarray:
    flat = values.reshape(-1)
    flat = flat[np.isfinite(flat)]

    if len(flat) <= max_pixels:
        return flat

    stride = max(1, math.ceil(len(flat) / max_pixels))
    return flat[::stride]


def _candidate_slope_stats(
    src: rasterio.io.DatasetReader,
    geom,
    threshold: float,
    max_pixels: int,
) -> dict[str, Any]:
    nodata = src.nodata

    try:
        arr, _ = raster_mask(
            src,
            [mapping(geom)],
            crop=True,
            filled=True,
            nodata=nodata if nodata is not None else -9999.0,
        )
    except ValueError:
        return {
            "valid_pixel_count": 0,
            "allowed_ratio": 0.0,
            "median_slope_deg": None,
            "p90_slope_deg": None,
            "keep": False,
        }

    data = arr[0].astype("float64")

    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    else:
        valid &= data != -9999.0

    values = data[valid]
    if len(values) == 0:
        return {
            "valid_pixel_count": 0,
            "allowed_ratio": 0.0,
            "median_slope_deg": None,
            "p90_slope_deg": None,
            "keep": False,
        }

    sampled = _sample_values(values, max_pixels=max_pixels)

    allowed_ratio = float(np.mean(sampled <= threshold))
    median_slope = float(np.median(sampled))
    p90_slope = float(np.percentile(sampled, 90))

    return {
        "valid_pixel_count": int(len(values)),
        "sampled_pixel_count": int(len(sampled)),
        "allowed_ratio": allowed_ratio,
        "median_slope_deg": median_slope,
        "p90_slope_deg": p90_slope,
        "keep": allowed_ratio >= 0.50,
    }


def apply_slope_screening(
    candidate_path: Path,
    slope_mask_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    candidates = _read_vector(candidate_path)

    manifest = json.loads(slope_mask_path.read_text(encoding="utf-8"))
    slope_raster_path = Path(manifest["slope_raster_path"])
    threshold = float(manifest["max_slope_deg"])
    max_pixels = int(manifest.get("max_pixels_per_candidate", 5000))

    kept_rows = []
    stats_rows = []

    with rasterio.open(slope_raster_path) as src:
        work = candidates
        original_crs = candidates.crs

        if work.crs is None and src.crs is not None:
            work = work.set_crs(src.crs, allow_override=True)

        if work.crs is not None and src.crs is not None and work.crs != src.crs:
            work = work.to_crs(src.crs)

        for idx, row in work.iterrows():
            stats = _candidate_slope_stats(
                src=src,
                geom=row.geometry,
                threshold=threshold,
                max_pixels=max_pixels,
            )

            stats_rows.append(stats)

            if stats["keep"]:
                kept_rows.append(idx)

        result = work.loc[kept_rows].copy()

        for field in [
            "allowed_ratio",
            "median_slope_deg",
            "p90_slope_deg",
            "valid_pixel_count",
            "sampled_pixel_count",
        ]:
            values = []
            stats_by_idx = dict(zip(work.index, stats_rows))
            for idx in result.index:
                values.append(stats_by_idx[idx].get(field))
            result[f"slope_{field}"] = values

        result["slope_threshold_deg"] = threshold
        result["slope_screening_method"] = "candidate_level_raster_statistics"

        if original_crs is not None and result.crs is not None and result.crs != original_crs:
            result = result.to_crs(original_crs)

    _write_vector(result, output_path)

    return {
        "artifact_path": str(output_path),
        "candidate_count": len(result),
        "input_candidate_count": len(candidates),
        "max_slope_deg": threshold,
        "screening_method": "candidate_level_raster_statistics",
        "allowed_ratio_threshold": 0.50,
    }

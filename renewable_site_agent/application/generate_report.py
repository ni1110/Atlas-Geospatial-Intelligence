"""Generate a deterministic, traceable HTML report for a site-selection job."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import jinja2
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.evidence import Evidence, EvidenceType
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.candidate_store import SQLiteCandidateStore
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


REPORT_GENERATOR_VERSION = "1.0.0"
REPORT_FILENAME = "site_selection_report.html"
REPORT_METADATA_FILENAME = "site_selection_report.metadata.json"


class ReportGenerationError(RuntimeError):
    """Raised when a report cannot be generated from verified persisted data."""


@dataclass(frozen=True)
class ReportGenerationResult:
    job_id: str
    report_path: Path
    metadata_path: Path
    report_sha256: str
    report_size_bytes: int
    candidate_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if is_dataclass(value):
        return dict(asdict(value))
    if isinstance(value, Mapping):
        return dict(value)
    raise ReportGenerationError(f"Unsupported structured value: {type(value).__name__}")


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _find_evidence(items: Iterable[Evidence], evidence_type: EvidenceType) -> Evidence:
    evidence = next((item for item in items if item.evidence_type == evidence_type), None)
    if evidence is None:
        raise ReportGenerationError(f"Missing required evidence: {evidence_type.value}")
    return evidence


def _resolve_verified_artifact(evidence: Evidence) -> Path:
    path = Path(evidence.artifact_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    project_root = PROJECT_ROOT.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ReportGenerationError(f"Artifact is outside project root: {resolved}") from exc
    if not resolved.is_file():
        raise ReportGenerationError(f"Artifact does not exist: {resolved}")
    actual = _sha256_file(resolved)
    if actual != evidence.checksum_sha256:
        raise ReportGenerationError(
            f"Artifact checksum mismatch for {evidence.evidence_type.value}: "
            f"{actual} != {evidence.checksum_sha256}"
        )
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportGenerationError(f"Unable to read JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReportGenerationError(f"Expected JSON object: {path}")
    return payload


def _read_feature_collection(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if payload.get("type") != "FeatureCollection":
        raise ReportGenerationError(f"Not a FeatureCollection: {path}")
    if not isinstance(payload.get("features"), list):
        raise ReportGenerationError(f"Invalid feature list: {path}")
    return payload


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _format_number(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(number)
    return f"{number:,.{digits}f}"


def _bool_text(value: Any) -> str:
    return "Yes" if bool(value) else "No"


def _screening_status(request: dict[str, Any]) -> list[dict[str, str]]:
    def pending_or_not_requested(flag: bool, note: str) -> dict[str, str]:
        if flag:
            return {"status": "Pending", "css_class": "pending", "note": note}
        return {"status": "Not requested", "css_class": "not-requested", "note": note}

    protected = pending_or_not_requested(
        bool(request.get("exclude_protected_areas")),
        "Requested by the user but not yet evaluated in the current workflow.",
    )
    farmland = pending_or_not_requested(
        bool(request.get("exclude_prime_farmland")),
        "Requested by the user but not yet evaluated in the current workflow.",
    )

    return [
        {
            "item": "Administrative boundary",
            "status": "Completed",
            "css_class": "completed",
            "note": "Beijing Municipality boundary persisted and checksum-verified.",
        },
        {
            "item": "Water exclusion",
            "status": "Completed",
            "css_class": "completed",
            "note": "OpenStreetMap water polygons clipped, unioned, and subtracted.",
        },
        {
            "item": "Candidate generation",
            "status": "Completed",
            "css_class": "completed",
            "note": "Preliminary non-water components generated; no business qualification applied.",
        },
        {
            "item": "Slope threshold",
            "status": "Pending",
            "css_class": "pending",
            "note": (
                f"User-specified threshold: < {request.get('max_slope_deg')}°. "
                "No terrain raster has been evaluated yet."
            ),
        },
        {
            "item": "Substation distance",
            "status": "Pending",
            "css_class": "pending",
            "note": (
                f"User-specified threshold: ≤ {request.get('max_substation_distance_km')} km. "
                "No substation network has been evaluated yet."
            ),
        },
        {"item": "Protected areas", **protected},
        {"item": "Prime farmland / land use", **farmland},
    ]


def _iter_polygon_rings(geometry: Mapping[str, Any]) -> Iterable[list[list[float]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        yield coordinates
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        for polygon in coordinates:
            if isinstance(polygon, list):
                yield polygon
    elif geometry_type == "GeometryCollection":
        for item in geometry.get("geometries", []) or []:
            if isinstance(item, Mapping):
                yield from _iter_polygon_rings(item)


def _svg_path(
    feature_collection: Mapping[str, Any],
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    width: int,
    height: int,
) -> str:
    span_x = east - west
    span_y = north - south
    if span_x <= 0 or span_y <= 0:
        raise ReportGenerationError("Invalid map bounds.")

    commands: list[str] = []
    for feature in feature_collection.get("features", []):
        geometry = feature.get("geometry") or {}
        for polygon in _iter_polygon_rings(geometry):
            for ring in polygon:
                points: list[str] = []
                for coordinate in ring:
                    if not isinstance(coordinate, list) or len(coordinate) < 2:
                        continue
                    lon, lat = float(coordinate[0]), float(coordinate[1])
                    x = (lon - west) / span_x * width
                    y = height - (lat - south) / span_y * height
                    points.append(f"{x:.2f},{y:.2f}")
                if len(points) >= 3:
                    commands.append("M " + " L ".join(points) + " Z")
    return " ".join(commands)


def _build_map_svg(
    boundary: Mapping[str, Any],
    water: Mapping[str, Any],
    candidates: Mapping[str, Any],
    bounding_box: list[Any],
) -> str:
    if len(bounding_box) != 4:
        raise ReportGenerationError("Boundary evidence does not contain a valid bounding box.")
    south, north, west, east = map(float, bounding_box)
    width, height = 1000, 600
    padding_x = (east - west) * 0.035
    padding_y = (north - south) * 0.035
    west -= padding_x
    east += padding_x
    south -= padding_y
    north += padding_y

    candidate_path = _svg_path(
        candidates, west=west, south=south, east=east, north=north, width=width, height=height
    )
    water_path = _svg_path(
        water, west=west, south=south, east=east, north=north, width=width, height=height
    )
    boundary_path = _svg_path(
        boundary, west=west, south=south, east=east, north=north, width=width, height=height
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Static site-screening map" style="display:block;width:100%;height:auto">'
        f'<rect width="{width}" height="{height}" fill="#111820"/>'
        f'<path d="{candidate_path}" fill="#5aae61" fill-opacity="0.72" '
        f'stroke="#2b7a3d" stroke-width="0.8" fill-rule="evenodd"/>'
        f'<path d="{water_path}" fill="#3182bd" fill-opacity="0.95" '
        f'stroke="#08519c" stroke-width="0.8" fill-rule="evenodd"/>'
        f'<path d="{boundary_path}" fill="none" stroke="#d7e9f4" '
        f'stroke-width="4" stroke-linejoin="round" fill-rule="evenodd"/>'
        f"</svg>"
    )


def _validate_preview_metadata(
    preview_metadata: dict[str, Any],
    *,
    boundary_evidence: Evidence,
    water_evidence: Evidence,
    candidate_evidence: Evidence,
    preview_directory: Path,
) -> dict[str, Path]:
    expected_sources = {
        "boundary": boundary_evidence.checksum_sha256,
        "water": water_evidence.checksum_sha256,
        "candidates": candidate_evidence.checksum_sha256,
    }
    source_artifacts = preview_metadata.get("source_artifacts", {})
    for key, expected_checksum in expected_sources.items():
        actual = source_artifacts.get(key, {}).get("sha256")
        if actual != expected_checksum:
            raise ReportGenerationError(f"Map preview is stale for source layer: {key}")

    paths = {
        "boundary": preview_directory / "boundary_preview.geojson",
        "water": preview_directory / "water_preview.geojson",
        "candidates": preview_directory / "candidate_preview.geojson",
    }
    preview_artifacts = preview_metadata.get("preview_artifacts", {})
    for key, path in paths.items():
        if not path.is_file():
            raise ReportGenerationError(f"Missing preview artifact: {path}")
        expected = preview_artifacts.get(key, {}).get("sha256")
        if not expected or _sha256_file(path) != expected:
            raise ReportGenerationError(f"Preview checksum mismatch: {key}")
    return paths


def generate_site_selection_report(
    job_id: str,
    *,
    store: SQLiteJobStore | None = None,
    candidate_store: SQLiteCandidateStore | None = None,
) -> ReportGenerationResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    store = store or SQLiteJobStore(database_path)
    candidate_store = candidate_store or SQLiteCandidateStore(database_path)

    job = store.get(job_id)
    if job.status != JobStatus.CANDIDATES_READY:
        raise ReportGenerationError(
            f"Report requires candidates_ready; received {_enum_value(job.status)}."
        )

    boundary_evidence = _find_evidence(job.evidence, EvidenceType.ADMINISTRATIVE_BOUNDARY)
    water_evidence = _find_evidence(job.evidence, EvidenceType.WATER_EXCLUSION)
    non_water_evidence = _find_evidence(job.evidence, EvidenceType.NON_WATER_AREA)
    candidate_evidence = _find_evidence(job.evidence, EvidenceType.CANDIDATE_SET)

    boundary_path = _resolve_verified_artifact(boundary_evidence)
    water_path = _resolve_verified_artifact(water_evidence)
    non_water_path = _resolve_verified_artifact(non_water_evidence)
    candidate_path = _resolve_verified_artifact(candidate_evidence)

    boundary_data = _read_feature_collection(boundary_path)
    _read_feature_collection(water_path)
    _read_feature_collection(non_water_path)
    candidate_data = _read_feature_collection(candidate_path)

    job_directory = RUNTIME_ROOT / "jobs" / job_id
    preview_directory = job_directory / "map_preview"
    preview_metadata_path = preview_directory / "preview_metadata.json"
    preview_metadata = _read_json(preview_metadata_path)
    if preview_metadata.get("job_id") != job_id:
        raise ReportGenerationError("Preview metadata job_id mismatch.")

    preview_paths = _validate_preview_metadata(
        preview_metadata,
        boundary_evidence=boundary_evidence,
        water_evidence=water_evidence,
        candidate_evidence=candidate_evidence,
        preview_directory=preview_directory,
    )
    boundary_preview = _read_feature_collection(preview_paths["boundary"])
    water_preview = _read_feature_collection(preview_paths["water"])
    candidate_preview = _read_feature_collection(preview_paths["candidates"])

    expected_candidate_count = int(candidate_evidence.metadata["candidate_count"])
    actual_candidate_count = len(candidate_data["features"])
    database_candidate_count = candidate_store.count_by_job(job_id)
    preview_candidate_count = len(candidate_preview["features"])

    if actual_candidate_count != expected_candidate_count:
        raise ReportGenerationError("Candidate artifact count does not match evidence metadata.")
    if database_candidate_count != expected_candidate_count:
        raise ReportGenerationError("Candidate database count does not match evidence metadata.")
    if preview_candidate_count != expected_candidate_count:
        raise ReportGenerationError("Candidate preview count does not match evidence metadata.")

    request = _as_mapping(job.request)
    candidate_properties = [
        feature.get("properties", {})
        for feature in candidate_data["features"]
        if isinstance(feature, dict)
    ]
    candidate_properties.sort(key=lambda item: int(item.get("rank", 10**12)))
    areas = [
        float(item["area_km2"])
        for item in candidate_properties
        if item.get("area_km2") is not None
    ]
    if not areas:
        raise ReportGenerationError("Candidate artifact contains no area values.")

    top_candidates = []
    for item in candidate_properties[:10]:
        lon = item.get("centroid_lon")
        lat = item.get("centroid_lat")
        centroid = (
            f"{float(lon):.6f}, {float(lat):.6f}"
            if lon is not None and lat is not None
            else "N/A"
        )
        top_candidates.append(
            {
                "rank": item.get("rank"),
                "candidate_id": item.get("candidate_id"),
                "area_km2": _format_number(item.get("area_km2"), 6),
                "centroid": centroid,
                "stage": item.get("stage"),
                "screening_status": item.get("screening_status"),
            }
        )

    boundary_meta = boundary_evidence.metadata
    non_water_meta = non_water_evidence.metadata
    candidate_meta = candidate_evidence.metadata

    map_svg = _build_map_svg(
        boundary_preview,
        water_preview,
        candidate_preview,
        list(boundary_meta.get("bounding_box", [])),
    )

    request_rows = [
        {"label": "Project type", "value": request.get("project_type"), "provenance": "User request"},
        {"label": "Region", "value": request.get("region"), "provenance": "User request"},
        {"label": "Capacity", "value": f"{request.get('capacity_mw')} MW", "provenance": "User request"},
        {"label": "Maximum slope", "value": f"{request.get('max_slope_deg')}°", "provenance": "User-specified threshold; not yet evaluated"},
        {"label": "Maximum substation distance", "value": f"{request.get('max_substation_distance_km')} km", "provenance": "User-specified threshold; not yet evaluated"},
        {"label": "Exclude protected areas", "value": _bool_text(request.get("exclude_protected_areas")), "provenance": "User request; not yet evaluated"},
        {"label": "Exclude water", "value": _bool_text(request.get("exclude_water")), "provenance": "User request; evaluated"},
        {"label": "Exclude prime farmland", "value": _bool_text(request.get("exclude_prime_farmland")), "provenance": "User request; not yet evaluated"},
    ]

    result_cards = [
        {"label": "Administrative area", "value": f"{_format_number(non_water_meta['boundary_area_km2'])} km²"},
        {"label": "Net excluded water", "value": f"{_format_number(non_water_meta['excluded_water_area_km2'])} km²"},
        {"label": "Remaining non-water area", "value": f"{_format_number(non_water_meta['result_area_km2'])} km²"},
        {"label": "Candidate components", "value": str(expected_candidate_count)},
        {"label": "Spatial overlap after difference", "value": f"{_format_number(non_water_meta['overlap_area_km2'], 9)} km²"},
        {"label": "Area conservation error", "value": f"{_format_number(candidate_meta['area_conservation_error_km2'], 9)} km²"},
    ]

    candidate_cards = [
        {"label": "Largest component", "value": f"{_format_number(max(areas), 6)} km²"},
        {"label": "Smallest component", "value": f"{_format_number(min(areas), 9)} km²"},
        {"label": "Mean component area", "value": f"{_format_number(statistics.fmean(areas), 6)} km²"},
        {"label": "Median component area", "value": f"{_format_number(statistics.median(areas), 6)} km²"},
    ]

    evidence_rows = [
        {
            "evidence_type": _enum_value(item.evidence_type),
            "provider": item.provider,
            "source_identifier": item.source_identifier,
            "source_license": item.source_license,
            "retrieved_at": str(item.retrieved_at),
            "checksum_sha256": item.checksum_sha256,
            "artifact_path": item.artifact_path,
        }
        for item in job.evidence
    ]

    template_directory = Path(__file__).resolve().parents[1] / "reporting" / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("site_selection_report.html.j2")

    generated_at = str(job.updated_at)
    html = template.render(
        job_id=job_id,
        job_status=_enum_value(job.status),
        request_text=job.request_text,
        request_rows=request_rows,
        screening_rows=_screening_status(request),
        result_cards=result_cards,
        map_svg=map_svg,
        candidate_cards=candidate_cards,
        top_candidates=top_candidates,
        evidence_rows=evidence_rows,
        generator_version=REPORT_GENERATOR_VERSION,
        generated_at=generated_at,
        metric_crs=candidate_meta.get("metric_crs"),
        tolerance_m=preview_metadata.get("tolerance_m"),
        source_total_mb=f"{int(preview_metadata['source_total_bytes']) / 1024 / 1024:.2f}",
        preview_total_mb=f"{int(preview_metadata['preview_total_bytes']) / 1024 / 1024:.2f}",
        reduction_percent=f"{float(preview_metadata['reduction_ratio']) * 100:.2f}",
    )

    reports_directory = job_directory / "reports"
    report_path = reports_directory / REPORT_FILENAME
    metadata_path = reports_directory / REPORT_METADATA_FILENAME
    _atomic_write_text(report_path, html)
    report_sha256 = _sha256_file(report_path)

    metadata = {
        "job_id": job_id,
        "generator_version": REPORT_GENERATOR_VERSION,
        "generated_at": generated_at,
        "report_path": str(report_path.relative_to(PROJECT_ROOT)),
        "report_sha256": report_sha256,
        "report_size_bytes": report_path.stat().st_size,
        "candidate_count": expected_candidate_count,
        "database_candidate_count": database_candidate_count,
        "input_evidence": [
            {
                "evidence_type": _enum_value(item.evidence_type),
                "artifact_path": item.artifact_path,
                "sha256": item.checksum_sha256,
            }
            for item in job.evidence
        ],
        "preview_metadata_sha256": _sha256_file(preview_metadata_path),
        "template_path": str((template_directory / "site_selection_report.html.j2").relative_to(PROJECT_ROOT)),
        "template_sha256": _sha256_file(template_directory / "site_selection_report.html.j2"),
        "generator_source_sha256": _sha256_file(Path(__file__).resolve()),
        "runtime_versions": {
            "python": sys.version.split()[0],
            "jinja2": jinja2.__version__,
        },
        "reproducibility_note": (
            "The report contains no wall-clock generation time. It uses job.updated_at so "
            "the same persisted inputs, template, code, and dependency versions reproduce "
            "the same HTML bytes."
        ),
    }
    _atomic_write_text(
        metadata_path,
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    return ReportGenerationResult(
        job_id=job_id,
        report_path=report_path,
        metadata_path=metadata_path,
        report_sha256=report_sha256,
        report_size_bytes=report_path.stat().st_size,
        candidate_count=expected_candidate_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="Persisted SiteSelectionJob identifier")
    args = parser.parse_args()
    result = generate_site_selection_report(args.job_id)
    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "report_path": str(result.report_path),
                "metadata_path": str(result.metadata_path),
                "report_sha256": result.report_sha256,
                "report_size_bytes": result.report_size_bytes,
                "candidate_count": result.candidate_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

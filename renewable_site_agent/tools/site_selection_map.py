"""Read-only map tool for persisted renewable-site screening jobs.

Full-resolution GeoJSON remains authoritative for analysis and audit. The live
map receives only checksum-verified preview GeoJSON generated from that source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoagent import geo_tool

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.evidence import Evidence, EvidenceType
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.candidate_store import SQLiteCandidateStore
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


CANDIDATE_PAINT: dict[str, Any] = {
    "fill-color": "#5aae61",
    "fill-opacity": 0.22,
    "fill-outline-color": "#2b7a3d",
}
WATER_PAINT: dict[str, Any] = {
    "fill-color": "#3182bd",
    "fill-opacity": 0.62,
    "fill-outline-color": "#08519c",
}
BOUNDARY_PAINT: dict[str, Any] = {
    "line-color": "#17365d",
    "line-width": 3,
    "line-opacity": 1.0,
}


class SiteSelectionMapError(RuntimeError):
    """Raised when a persisted job cannot be displayed safely."""


@dataclass(frozen=True)
class LayerSpec:
    name: str
    data: dict[str, Any]
    layer_type: str
    paint: dict[str, Any]


@dataclass(frozen=True)
class PreviewBundle:
    data: dict[str, dict[str, Any]]
    source_counts: dict[str, int]
    preview_counts: dict[str, int]
    source_total_bytes: int
    preview_total_bytes: int
    reduction_ratio: float
    tolerance_m: float | None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_evidence(items: list[Evidence], kind: EvidenceType) -> Evidence:
    evidence = next((item for item in items if item.evidence_type == kind), None)
    if evidence is None:
        raise SiteSelectionMapError(f"Missing required evidence: {kind.value}")
    return evidence


def _resolve_project_file(path_value: str | Path, label: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise SiteSelectionMapError(
            f"{label} is outside the project directory: {resolved}"
        ) from exc

    if not resolved.is_file():
        raise SiteSelectionMapError(f"{label} does not exist: {resolved}")
    return resolved


def _resolve_verified_evidence(evidence: Evidence) -> Path:
    path = _resolve_project_file(
        evidence.artifact_path,
        f"Evidence artifact {evidence.evidence_type.value}",
    )
    if _sha256_file(path) != evidence.checksum_sha256:
        raise SiteSelectionMapError(
            f"Evidence checksum validation failed: {evidence.evidence_type.value}"
        )
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteSelectionMapError(f"Unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise SiteSelectionMapError(f"{label} must contain a JSON object: {path}")
    return value


def _read_feature_collection(path: Path) -> dict[str, Any]:
    value = _read_json(path, "GeoJSON")
    if value.get("type") != "FeatureCollection":
        raise SiteSelectionMapError(f"Not a GeoJSON FeatureCollection: {path}")
    if not isinstance(value.get("features"), list):
        raise SiteSelectionMapError(f"Invalid GeoJSON features: {path}")
    return value


def _require_dict(value: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise SiteSelectionMapError(f"{label} is missing object field: {key}")
    return result


def _require_str(value: dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise SiteSelectionMapError(f"{label} is missing text field: {key}")
    return result


def _require_int(value: dict[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise SiteSelectionMapError(f"{label} has invalid integer field: {key}")
    return result


def _load_verified_preview(
    job_id: str,
    evidence_by_layer: dict[str, Evidence],
    source_paths: dict[str, Path],
) -> PreviewBundle:
    preview_dir = (RUNTIME_ROOT / "jobs" / job_id / "map_preview").resolve()
    manifest_path = preview_dir / "preview_metadata.json"
    if not manifest_path.is_file():
        raise SiteSelectionMapError(
            "Map preview is missing. Run build_map_preview(job_id) first."
        )

    manifest = _read_json(manifest_path, "map preview metadata")
    if manifest.get("job_id") != job_id:
        raise SiteSelectionMapError("Map preview metadata belongs to another job.")

    source_records = _require_dict(
        manifest, "source_artifacts", "map preview metadata"
    )
    preview_records = _require_dict(
        manifest, "preview_artifacts", "map preview metadata"
    )

    data: dict[str, dict[str, Any]] = {}
    source_counts: dict[str, int] = {}
    preview_counts: dict[str, int] = {}

    for layer_key in ("boundary", "water", "candidates"):
        evidence = evidence_by_layer[layer_key]

        source_record = _require_dict(
            source_records, layer_key, "source_artifacts"
        )
        manifest_source = _resolve_project_file(
            _require_str(source_record, "path", f"source_artifacts.{layer_key}"),
            f"Manifest source artifact {layer_key}",
        )
        manifest_source_sha = _require_str(
            source_record, "sha256", f"source_artifacts.{layer_key}"
        )
        source_counts[layer_key] = _require_int(
            source_record, "feature_count", f"source_artifacts.{layer_key}"
        )

        if manifest_source != source_paths[layer_key]:
            # The candidates preview may be generated from the available-data
            # final candidate file, while the original evidence still points to
            # the preliminary candidate_sites.geojson. Accept this case only
            # when the final GeoJSON exists and its checksum matches the
            # decision log. Do not continue here; the layer still needs to be
            # loaded into preview.data["candidates"].
            allow_final_candidate_preview = False

            if layer_key == "candidates":
                final_candidate_file = (
                    RUNTIME_ROOT
                    / "jobs"
                    / job_id
                    / "final"
                    / "available_data_screened_candidate_sites.geojson"
                )
                final_decision_log = (
                    RUNTIME_ROOT
                    / "jobs"
                    / job_id
                    / "final"
                    / "screening_decision_log.json"
                )

                if final_candidate_file.is_file() and final_decision_log.is_file():
                    decision_log = json.loads(
                        final_decision_log.read_text(encoding="utf-8")
                    )
                    final_summary = decision_log.get("latest_candidate_output")
                    if isinstance(final_summary, dict):
                        expected_final_sha256 = final_summary.get("sha256")
                        manifest_matches_final = (
                            manifest_source == final_candidate_file
                        )
                        checksum_matches = (
                            expected_final_sha256
                            and _local_sha256_file(final_candidate_file)
                            == expected_final_sha256
                        )
                        allow_final_candidate_preview = bool(
                            manifest_matches_final and checksum_matches
                        )

            if allow_final_candidate_preview:
                source_paths[layer_key] = manifest_source
            else:
                raise SiteSelectionMapError(
                    f"Map preview source path is stale: {layer_key}"
                )
        if manifest_source_sha != evidence.checksum_sha256:
            raise SiteSelectionMapError(
                f"Map preview source checksum is stale: {layer_key}"
            )

        preview_record = _require_dict(
            preview_records, layer_key, "preview_artifacts"
        )
        preview_path = _resolve_project_file(
            _require_str(preview_record, "path", f"preview_artifacts.{layer_key}"),
            f"Map preview artifact {layer_key}",
        )
        try:
            preview_path.relative_to(preview_dir)
        except ValueError as exc:
            raise SiteSelectionMapError(
                f"Preview artifact is outside its job directory: {layer_key}"
            ) from exc

        expected_preview_sha = _require_str(
            preview_record, "sha256", f"preview_artifacts.{layer_key}"
        )
        expected_preview_count = _require_int(
            preview_record, "feature_count", f"preview_artifacts.{layer_key}"
        )
        if _sha256_file(preview_path) != expected_preview_sha:
            raise SiteSelectionMapError(
                f"Map preview checksum validation failed: {layer_key}"
            )

        layer_data = _read_feature_collection(preview_path)
        actual_preview_count = len(layer_data["features"])
        if actual_preview_count != expected_preview_count:
            raise SiteSelectionMapError(
                "Map preview count does not match metadata: "
                f"{layer_key}: {actual_preview_count} != {expected_preview_count}"
            )

        data[layer_key] = layer_data
        preview_counts[layer_key] = actual_preview_count

    source_total_bytes = manifest.get("source_total_bytes")
    preview_total_bytes = manifest.get("preview_total_bytes")
    reduction_ratio = manifest.get("reduction_ratio")
    tolerance_m = manifest.get("tolerance_m")

    if not isinstance(source_total_bytes, int) or source_total_bytes < 0:
        raise SiteSelectionMapError("Invalid preview source_total_bytes metadata.")
    if not isinstance(preview_total_bytes, int) or preview_total_bytes < 0:
        raise SiteSelectionMapError("Invalid preview preview_total_bytes metadata.")
    if not isinstance(reduction_ratio, (int, float)):
        raise SiteSelectionMapError("Invalid preview reduction_ratio metadata.")
    if tolerance_m is not None and not isinstance(tolerance_m, (int, float)):
        raise SiteSelectionMapError("Invalid preview tolerance_m metadata.")

    return PreviewBundle(
        data=data,
        source_counts=source_counts,
        preview_counts=preview_counts,
        source_total_bytes=source_total_bytes,
        preview_total_bytes=preview_total_bytes,
        reduction_ratio=float(reduction_ratio),
        tolerance_m=float(tolerance_m) if tolerance_m is not None else None,
    )


def _existing_layer_names(map_obj: Any) -> set[str]:
    names = set(getattr(map_obj, "_site_selection_layer_names", set()))
    for layer in getattr(map_obj, "layers", None) or []:
        if isinstance(layer, dict):
            name = layer.get("name") or layer.get("layer_name") or layer.get("id")
        else:
            name = (
                getattr(layer, "name", None)
                or getattr(layer, "layer_name", None)
                or getattr(layer, "id", None)
            )
        if name:
            names.add(str(name))
    return names


def _remember_layer(map_obj: Any, name: str) -> None:
    names = set(getattr(map_obj, "_site_selection_layer_names", set()))
    names.add(name)
    try:
        setattr(map_obj, "_site_selection_layer_names", names)
    except (AttributeError, TypeError):
        pass


def _add_layer(map_obj: Any, spec: LayerSpec) -> bool:
    if spec.name in _existing_layer_names(map_obj):
        return False

    add_geojson = getattr(map_obj, "add_geojson", None)
    if not callable(add_geojson):
        raise SiteSelectionMapError(
            f"{type(map_obj).__name__} does not support add_geojson."
        )

    try:
        add_geojson(
            spec.data,
            layer_type=spec.layer_type,
            paint=spec.paint,
            name=spec.name,
            fit_bounds=False,
            visible=True,
            opacity=1.0,
        )
    except TypeError as exc:
        raise SiteSelectionMapError(
            "Installed AnyMap add_geojson API is incompatible with "
            "layer_type/paint/name arguments."
        ) from exc

    _remember_layer(map_obj, spec.name)
    return True


def _fit_boundary(map_obj: Any, boundary_evidence: Evidence) -> None:
    bbox = boundary_evidence.metadata.get("bounding_box")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return

    south, north, west, east = map(float, bbox)
    bounds = [[west, south], [east, north]]

    fit_bounds = getattr(map_obj, "fit_bounds", None)
    if callable(fit_bounds):
        fit_bounds(bounds)
        return

    zoom_to_bounds = getattr(map_obj, "zoom_to_bounds", None)
    if callable(zoom_to_bounds):
        try:
            zoom_to_bounds(bounds)
        except TypeError:
            zoom_to_bounds(west, south, east, north)


def _local_sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_site_selection_layers(map_obj: Any, job_id: str) -> dict[str, Any]:
    """Validate one persisted job and display its lightweight preview layers."""

    database_path = RUNTIME_ROOT / "site_jobs.db"
    job = SQLiteJobStore(database_path).get(job_id)
    candidate_store = SQLiteCandidateStore(database_path)

    if job.status != JobStatus.CANDIDATES_READY:
        raise SiteSelectionMapError(
            "Map display requires job status candidates_ready; "
            f"received {job.status.value}."
        )

    evidence_by_layer = {
        "boundary": _find_evidence(
            job.evidence, EvidenceType.ADMINISTRATIVE_BOUNDARY
        ),
        "water": _find_evidence(job.evidence, EvidenceType.WATER_EXCLUSION),
        "candidates": _find_evidence(job.evidence, EvidenceType.CANDIDATE_SET),
    }
    source_paths = {
        key: _resolve_verified_evidence(evidence)
        for key, evidence in evidence_by_layer.items()
    }
    preview = _load_verified_preview(job_id, evidence_by_layer, source_paths)

    candidate_evidence = evidence_by_layer["candidates"]
    try:
        expected_candidate_count = int(
            candidate_evidence.metadata["candidate_count"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SiteSelectionMapError(
            "Candidate evidence has invalid candidate_count metadata."
        ) from exc

    database_candidate_count = candidate_store.count_by_job(job_id)

    preview_candidate_stage = "preliminary_non_water"
    preview_candidate_screening_status = "not_evaluated"
    expected_preview_candidate_count = expected_candidate_count
    database_count_check = "checked_against_original_candidate_store"

    final_decision_log = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "final"
        / "screening_decision_log.json"
    )
    final_candidate_file = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "final"
        / "available_data_screened_candidate_sites.geojson"
    )

    if final_decision_log.is_file() and final_candidate_file.is_file():
        decision_log = json.loads(
            final_decision_log.read_text(encoding="utf-8")
        )
        final_summary = decision_log["latest_candidate_output"]
        expected_final_sha256 = final_summary["sha256"]

        if _local_sha256_file(final_candidate_file) != expected_final_sha256:
            raise RuntimeError(
                "Final candidate checksum does not match decision log."
            )

        expected_preview_candidate_count = int(final_summary["candidate_count"])
        preview_candidate_stage = "available_data_screened_preliminary_candidates"
        preview_candidate_screening_status = decision_log.get(
            "result_status",
            "preliminary_not_final_recommendation",
        )
        database_count_check = (
            "skipped_for_available_data_final_preview; "
            "database stores original preliminary candidates"
        )

    if preview.source_counts["candidates"] != expected_preview_candidate_count:
        raise RuntimeError(
            "Candidate preview source count mismatch: "
            f'{preview.source_counts["candidates"]} != {expected_preview_candidate_count}'
        )

    if (
        expected_preview_candidate_count == expected_candidate_count
        and database_candidate_count != expected_candidate_count
    ):
        raise RuntimeError(
            "Candidate database count mismatch: "
            f"{database_candidate_count} != {expected_candidate_count}"
        )

    boundary_evidence = evidence_by_layer["boundary"]
    water_evidence = evidence_by_layer["water"]
    short_job_id = job_id.split("-")[0]

    # Later MapLibre layers render above earlier layers.
    specs = [
        LayerSpec(
            name=(
                f"Preliminary Candidates | {short_job_id} | "
                f"{candidate_evidence.checksum_sha256[:8]}"
            ),
            data=preview.data["candidates"],
            layer_type="fill",
            paint=CANDIDATE_PAINT,
        ),
        LayerSpec(
            name=(
                f"Excluded Water | {short_job_id} | "
                f"{water_evidence.checksum_sha256[:8]}"
            ),
            data=preview.data["water"],
            layer_type="fill",
            paint=WATER_PAINT,
        ),
        LayerSpec(
            name=(
                f"Administrative Boundary | {short_job_id} | "
                f"{boundary_evidence.checksum_sha256[:8]}"
            ),
            data=preview.data["boundary"],
            layer_type="line",
            paint=BOUNDARY_PAINT,
        ),
    ]

    loaded_layers: list[str] = []
    skipped_layers: list[str] = []
    for spec in specs:
        if _add_layer(map_obj, spec):
            loaded_layers.append(spec.name)
        else:
            skipped_layers.append(spec.name)

    _fit_boundary(map_obj, boundary_evidence)

    return {
        "success": True,
        "job_id": job_id,
        "job_status": job.status.value,
        "boundary_feature_count": preview.source_counts["boundary"],
        "water_feature_count": preview.source_counts["water"],
        "candidate_feature_count": preview.source_counts["candidates"],
        "database_candidate_count": database_candidate_count,
        "database_count_check": database_count_check,
        "preview_boundary_feature_count": preview.preview_counts["boundary"],
        "preview_water_feature_count": preview.preview_counts["water"],
        "preview_candidate_feature_count": preview.preview_counts["candidates"],
        "preview_source_total_mb": round(
            preview.source_total_bytes / 1024 / 1024, 2
        ),
        "preview_render_total_mb": round(
            preview.preview_total_bytes / 1024 / 1024, 2
        ),
        "preview_reduction_percent": round(preview.reduction_ratio * 100, 2),
        "preview_tolerance_m": preview.tolerance_m,
        "loaded_layers": loaded_layers,
        "skipped_existing_layers": skipped_layers,
        "candidate_stage": preview_candidate_stage,
        "candidate_screening_status": preview_candidate_screening_status,
        "warning": (
            "These polygons are available-data preliminary screening units, not "
            "approved or recommended renewable-energy sites. Map previews are "
            "visualization derivatives; full-resolution evidence remains "
            "authoritative."
        ),
    }


def site_selection_map_tools(map_obj: Any) -> list[Any]:
    """Build renewable-site display tools bound to a live map."""

    if map_obj is None:
        return []

    @geo_tool(category="renewable_site", available_in=("full", "fast"))
    def show_site_selection_layers(job_id: str) -> dict[str, Any]:
        """
        Display one verified site-selection job on the live map.

        This read-only tool verifies the job, authoritative evidence checksums,
        preview lineage/checksums, and candidate database count before rendering.

        Args:
            job_id: Persisted SiteSelectionJob identifier.

        Returns:
            Source/preview counts, loaded layers, compression statistics, job
            status, and a warning that these are not approved site recommendations.
        """
        return load_site_selection_layers(map_obj, job_id)

    return [show_site_selection_layers]

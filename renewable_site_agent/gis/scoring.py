"""Deterministic project-specific candidate scoring and Top-K ranking."""

from __future__ import annotations

import math
from typing import Any

from renewable_site_agent.config import load_config
from renewable_site_agent.schemas import CandidateResult, DataArtifact, SiteSelectionRequest


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _distance_score(distance: float | None, hard_limit: float | None, decay: float) -> float | None:
    if distance is None:
        return None
    if hard_limit is not None:
        return _clamp(100 * (1 - distance / hard_limit))
    return _clamp(100 * math.exp(-distance / decay))


def capacity_area_check(request: SiteSelectionRequest) -> dict[str, Any]:
    config = load_config("scoring.yaml")
    factors = config.get("area_per_mw_km2", {})
    factor = factors.get(request.project_type)
    if request.capacity_mw is None:
        return {"status": "not_requested", "required_area_km2": None}
    if factor is None:
        return {
            "status": "unavailable",
            "required_area_km2": None,
            "reason": f"No confirmed area_per_mw_km2 configured for {request.project_type}",
        }
    return {
        "status": "available",
        "area_per_mw_km2": float(factor),
        "required_area_km2": float(factor) * request.capacity_mw,
    }


def score_and_rank(
    candidates_geojson: dict[str, Any],
    request: SiteSelectionRequest,
    provenance: list[DataArtifact],
) -> list[CandidateResult]:
    config = load_config("scoring.yaml")
    weights = config["weights"][request.project_type]
    decay = config.get("distance_decay_km", {"grid": 20, "road": 10})
    features = candidates_geojson.get("features", [])
    maximum_area = max(
        (float(feature.get("properties", {}).get("area_km2", 0)) for feature in features),
        default=0,
    )
    area_check = capacity_area_check(request)
    values: list[CandidateResult] = []
    for feature in features:
        properties = feature.get("properties", {})
        area = float(properties.get("area_km2", 0))
        mean_slope = properties.get("mean_slope_deg")
        slope_score = None
        if mean_slope is not None and request.max_slope_deg:
            slope_score = _clamp(100 * (1 - float(mean_slope) / request.max_slope_deg))
        energy_score = properties.get("energy_resource_score")
        energy_score = _clamp(energy_score) if energy_score is not None else None
        substation_distance = properties.get("substation_distance_km")
        road_distance = properties.get("road_distance_km")
        grid_score = _distance_score(
            substation_distance, request.max_substation_distance_km, float(decay["grid"])
        )
        accessibility_score = _distance_score(
            road_distance, request.max_road_distance_km, float(decay["road"])
        )
        land_cover_score = properties.get("land_cover_score")
        land_cover_score = _clamp(land_cover_score) if land_cover_score is not None else None
        required = area_check.get("required_area_km2")
        reference_area = required if required else maximum_area
        area_score = _clamp(100 * area / reference_area) if reference_area else None
        components = {
            "slope_score": slope_score,
            "energy_resource_score": energy_score,
            "grid_score": grid_score,
            "accessibility_score": accessibility_score,
            "land_cover_score": land_cover_score,
            "area_score": area_score,
        }
        available_weight = sum(float(weights[key]) for key, value in components.items() if value is not None)
        total_weight = sum(float(value) for value in weights.values() if float(value) > 0)
        overall = (
            sum(float(weights[key]) * float(value) for key, value in components.items() if value is not None)
            / available_weight
            if available_weight
            else 0
        )
        completeness = 100 * available_weight / total_weight if total_weight else 100
        missing = [key for key, value in components.items() if value is None and float(weights[key]) > 0]
        strengths = [key for key, value in components.items() if value is not None and value >= 75]
        risks = [f"missing:{key}" for key in missing]
        if area_check["status"] == "available" and area < float(required):
            risks.append("capacity_area_insufficient")
        if area_check["status"] == "unavailable" and request.capacity_mw is not None:
            risks.append("capacity_area_check_unavailable")
        values.append(
            CandidateResult(
                candidate_id=str(properties.get("candidate_id")), area_km2=area,
                slope_score=slope_score, energy_resource_score=energy_score,
                substation_distance_km=substation_distance, grid_score=grid_score,
                road_distance_km=road_distance, accessibility_score=accessibility_score,
                land_cover_score=land_cover_score, area_score=area_score,
                overall_score=_clamp(overall),
                score_breakdown={
                    "weights": weights, "components": components,
                    "missing_evidence_policy": config["missing_evidence_policy"],
                    "missing_components": missing, "capacity_area_check": area_check,
                },
                evidence_completeness_score=_clamp(completeness),
                key_strengths=strengths, key_risks=risks,
                constraint_evidence=properties.get("constraint_evidence", {}),
                data_provenance=provenance, geometry=feature["geometry"],
            )
        )
    values.sort(key=lambda candidate: (-candidate.overall_score, -candidate.area_km2, candidate.candidate_id))
    for rank, candidate in enumerate(values[: request.top_k], 1):
        candidate.rank = rank
    return values[: request.top_k]

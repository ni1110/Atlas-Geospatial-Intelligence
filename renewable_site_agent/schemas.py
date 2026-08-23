"""Pydantic contracts shared by the canonical Agent, tools, and API."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


IntentName = Literal[
    "renewable_site_selection",
    "terrain_analysis",
    "remote_sensing_analysis",
    "general_gis",
    "geospatial_data_query",
    "unknown",
]
ProjectType = Literal["solar", "wind", "storage", "hybrid", "unknown"]


class IntentDecision(BaseModel):
    intent: IntentName
    subtype: ProjectType = "unknown"
    confidence: float = Field(ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    selected_skill: Literal[
        "site-selection",
        "terrain-analysis",
        "remote-sensing",
        "general-gis",
        "none",
    ]
    route_mode: Literal["llm", "deterministic_fallback"] = "llm"
    reason: str = ""


class AOI(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: tuple[float, float, float, float] | None = None
    geojson: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_one_geometry(self) -> "AOI":
        if self.bbox is None and self.geojson is None:
            raise ValueError("AOI requires bbox or geojson")
        if self.bbox is not None:
            west, south, east, north = self.bbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("AOI bbox must be ordered W,S,E,N in EPSG:4326")
        return self


class SiteSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str | None = None
    aoi: AOI | None = None
    project_type: ProjectType = "unknown"
    capacity_mw: float | None = Field(default=None, gt=0)
    max_slope_deg: float | None = Field(default=8.0, ge=0, le=90)
    exclude_water: bool = True
    exclude_protected_area: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "exclude_protected_area", "exclude_protected_areas"
        ),
    )
    exclude_prime_farmland: bool = False
    excluded_land_cover_classes: list[int] = Field(default_factory=list)
    land_cover_preferences: dict[str, float] = Field(default_factory=dict)
    max_substation_distance_km: float | None = Field(default=None, gt=0)
    max_road_distance_km: float | None = Field(default=None, gt=0)
    minimum_contiguous_area_km2: float | None = Field(default=None, gt=0)
    top_k: int = Field(default=5, ge=1, le=20)
    require_remote_sensing: bool = False
    require_vlm_verification: bool = False

    @model_validator(mode="after")
    def normalize_and_validate(self) -> "SiteSelectionRequest":
        if not self.region and not self.aoi:
            raise ValueError("region or AOI is required")
        if self.project_type == "unknown":
            raise ValueError("project_type is required")
        return self

    @property
    def exclude_protected_areas(self) -> bool:
        """Compatibility alias for the historical plural field."""
        return self.exclude_protected_area

    @property
    def hard_constraints(self) -> dict[str, Any]:
        return {
            "aoi": self.aoi.model_dump() if self.aoi else None,
            "exclude_water": self.exclude_water,
            "exclude_protected_area": self.exclude_protected_area,
            "max_slope_deg": self.max_slope_deg,
            "excluded_land_cover_classes": self.excluded_land_cover_classes,
            "max_substation_distance_km": self.max_substation_distance_km,
            "max_road_distance_km": self.max_road_distance_km,
            "minimum_contiguous_area_km2": self.minimum_contiguous_area_km2,
        }


class ErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSIENT_NETWORK_ERROR = "TRANSIENT_NETWORK_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    DATA_NOT_FOUND = "DATA_NOT_FOUND"
    GEOMETRY_ERROR = "GEOMETRY_ERROR"
    PROJECTION_ERROR = "PROJECTION_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    FATAL_ERROR = "FATAL_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    LOOP_DETECTED = "LOOP_DETECTED"


class ToolError(BaseModel):
    error_code: ErrorCode
    retryable: bool
    fallback_allowed: bool
    message: str
    provider: str | None = None


class DataArtifact(BaseModel):
    provider: str
    dataset: str
    dataset_version: str
    source_url: str | None = None
    aoi: dict[str, Any] | None = None
    bbox: tuple[float, float, float, float] | None = None
    crs: str
    resolution: str | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    license: str
    attribution: str
    checksum_sha256: str | None = None
    local_path: str | None = None
    quality_flag: Literal["valid", "degraded", "unavailable"] = "valid"
    cache_hit: bool = False
    source_degraded: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    status: Literal["success", "degraded", "failed"]
    error_code: ErrorCode | None = None
    retryable: bool = False
    message: str = ""
    artifact: DataArtifact | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: list[DataArtifact] = Field(default_factory=list)
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    degraded: bool = False
    fallback_reason: str | None = None
    original_provider: str | None = None
    fallback_provider: str | None = None

    @classmethod
    def ok(cls, *, message: str = "", **kwargs: Any) -> "ToolResult":
        return cls(status="success", message=message, **kwargs)

    @classmethod
    def fail(
        cls,
        error_code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        **kwargs: Any,
    ) -> "ToolResult":
        return cls(
            status="failed",
            error_code=error_code,
            message=message,
            retryable=retryable,
            **kwargs,
        )


class ToolPlanStep(BaseModel):
    step_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None
    fallback_tool: str | None = None


class ToolPlan(BaseModel):
    selected_skill: str
    steps: list[ToolPlanStep]
    planner_mode: Literal["llm", "deterministic_fallback"] = "llm"

    @field_validator("steps")
    @classmethod
    def non_empty_steps(cls, value: list[ToolPlanStep]) -> list[ToolPlanStep]:
        if not value:
            raise ValueError("Tool plan cannot be empty")
        return value


class CandidateResult(BaseModel):
    rank: int = 0
    candidate_id: str
    area_km2: float = Field(ge=0)
    slope_score: float | None = Field(default=None, ge=0, le=100)
    energy_resource_score: float | None = Field(default=None, ge=0, le=100)
    substation_distance_km: float | None = Field(default=None, ge=0)
    grid_score: float | None = Field(default=None, ge=0, le=100)
    road_distance_km: float | None = Field(default=None, ge=0)
    accessibility_score: float | None = Field(default=None, ge=0, le=100)
    land_cover_score: float | None = Field(default=None, ge=0, le=100)
    area_score: float | None = Field(default=None, ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)
    score_breakdown: dict[str, Any]
    evidence_completeness_score: float = Field(ge=0, le=100)
    key_strengths: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    constraint_evidence: dict[str, Any] = Field(default_factory=dict)
    data_provenance: list[DataArtifact] = Field(default_factory=list)
    geometry: dict[str, Any]

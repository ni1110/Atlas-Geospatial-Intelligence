"""Request parser and scope resolver for request-driven renewable-site Agent.

This module converts a user natural-language request into:
1. a structured renewable-site selection request;
2. a current-scope decision;
3. a dynamic tool requirement list.

It supports general renewable-site requests, solar, wind, storage, and hybrid
project wording. GIS execution is added in the next layer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


SUPPORTED_PROJECT_TYPES = {
    "renewable_site": {
        "zh": "新能源场站",
        "family": "renewable_energy",
        "keywords": (
            "新能源站",
            "新能源场站",
            "新能源项目",
            "新能源选址",
            "新能源",
            "renewable site",
            "renewable energy",
            "renewable project",
        ),
    },
    "solar": {
        "zh": "光伏",
        "family": "renewable_energy",
        "keywords": ("光伏", "太阳能", "solar", "photovoltaic", "pv"),
    },
    "wind": {
        "zh": "风电",
        "family": "renewable_energy",
        "keywords": ("风电", "风力", "风场", "wind", "wind farm"),
    },
    "storage": {
        "zh": "储能",
        "family": "renewable_energy",
        "keywords": ("储能", "电化学储能", "储能电站", "battery storage", "energy storage", "bess"),
    },
    "hybrid": {
        "zh": "风光储 / 综合能源",
        "family": "renewable_energy",
        "keywords": (
            "风光储",
            "光储",
            "风储",
            "综合能源",
            "多能互补",
            "hybrid",
            "solar wind storage",
        ),
    },
}

SUPPORTED_REGIONS = {
    "北京市": ("北京市", "北京", "beijing"),
}

OUT_OF_SCOPE_CONSTRAINTS = {
    "max_distance_to_substation_km": {
        "status": "out_of_scope_by_current_product_version",
        "status_zh": "当前产品版本暂不支持",
        "reason_zh": "变电站距离筛选尚未接入请求驱动执行链。",
    },
    "exclude_prime_farmland": {
        "status": "out_of_scope_by_current_product_version",
        "status_zh": "当前产品版本暂不支持",
        "reason_zh": "基本农田 / 土地利用筛选尚未接入请求驱动执行链。",
    },
    "solar_irradiance": {
        "status": "out_of_scope_by_current_product_version",
        "status_zh": "当前产品版本暂不支持",
        "reason_zh": "光照资源筛选尚未接入请求驱动执行链。",
    },
    "wind_resource": {
        "status": "out_of_scope_by_current_product_version",
        "status_zh": "当前产品版本暂不支持",
        "reason_zh": "风资源筛选尚未接入请求驱动执行链。",
    },
    "grid_connection_cost": {
        "status": "out_of_scope_by_current_product_version",
        "status_zh": "当前产品版本暂不支持",
        "reason_zh": "电网接入成本筛选尚未接入请求驱动执行链。",
    },
}

CONSTRAINT_TOOLS = {
    "administrative_boundary": ["load_administrative_boundary"],
    "exclude_water": ["exclude_water_bodies"],
    "max_slope_deg": [
        "prepare_terrain_dem",
        "calculate_slope",
        "vectorize_slope_mask",
        "apply_slope_screening",
    ],
    "exclude_protected_areas": [
        "prepare_protected_areas",
        "apply_protected_area_screening",
    ],
}


@dataclass(frozen=True)
class ParsedSiteSelectionRequest:
    request_id: str
    raw_text: str
    project_family: str | None
    project_type: str | None
    project_type_zh: str | None
    region: str | None
    capacity_mw: float | None
    exclude_water: bool
    max_slope_deg: float | None
    exclude_protected_areas: bool
    exclude_prime_farmland: bool
    max_distance_to_substation_km: float | None
    request_solar_irradiance: bool
    request_wind_resource: bool
    request_grid_connection_cost: bool
    parser_mode: str


def _make_request_id(raw_text: str) -> str:
    digest = hashlib.sha1(raw_text.encode("utf-8")).hexdigest()[:12]
    return f"request-{digest}"


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _parse_project_type(text: str) -> tuple[str | None, str | None, str | None]:
    # Prefer specific types over generic renewable wording.
    priority = ["hybrid", "storage", "wind", "solar", "renewable_site"]
    for project_type in priority:
        spec = SUPPORTED_PROJECT_TYPES[project_type]
        if _contains_any(text, spec["keywords"]):
            return spec["family"], project_type, spec["zh"]

    # If the user talks about site selection but does not name a technology,
    # default to a general renewable-site task rather than forcing solar.
    if _contains_any(text, ("选址", "候选区域", "场址", "site selection", "candidate site")):
        spec = SUPPORTED_PROJECT_TYPES["renewable_site"]
        return spec["family"], "renewable_site", spec["zh"]

    return None, None, None


def _parse_region(text: str) -> str | None:
    lowered = text.lower()
    for normalized_region, keywords in SUPPORTED_REGIONS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            return normalized_region
    return None


def _parse_capacity_mw(text: str) -> float | None:
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(?:mw|MW)",
        r"(\d+(?:\.\d+)?)\s*(?:兆瓦)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _parse_slope_deg(text: str) -> float | None:
    patterns = [
        r"坡度\s*(?:小于|低于|不超过|不大于|<=|<|≤)\s*(\d+(?:\.\d+)?)\s*(?:度|°)?",
        r"slope\s*(?:less than|under|below|<=|<)\s*(\d+(?:\.\d+)?)",
        r"max(?:imum)?\s*slope\s*(?:of|=|<=|<)?\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _parse_substation_distance_km(text: str) -> float | None:
    patterns = [
        r"变电站.{0,12}(?:小于|低于|不超过|不大于|<=|<|≤)\s*(\d+(?:\.\d+)?)\s*(?:km|公里|千米)",
        r"substation.{0,24}(?:less than|under|below|<=|<)\s*(\d+(?:\.\d+)?)\s*(?:km|kilometers?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def parse_request_text(raw_text: str) -> ParsedSiteSelectionRequest:
    text = raw_text.strip()
    if not text:
        raise ValueError("Empty request text.")

    project_family, project_type, project_type_zh = _parse_project_type(text)

    exclude_water = _contains_any(
        text,
        ("避开水体", "排除水体", "远离水体", "不要水体", "水体", "water", "river", "lake"),
    )
    exclude_protected = _contains_any(
        text,
        ("保护区", "自然保护区", "生态红线", "protected area", "nature reserve"),
    )
    exclude_prime_farmland = _contains_any(
        text,
        ("基本农田", "永久基本农田", "农田", "farmland", "prime farmland"),
    )

    request_solar_irradiance = _contains_any(
        text,
        ("光照", "太阳辐照", "辐照度", "日照", "irradiance", "solar resource"),
    )
    request_wind_resource = _contains_any(
        text,
        ("风速", "风资源", "wind speed", "wind resource"),
    )
    request_grid_connection_cost = _contains_any(
        text,
        ("电网接入成本", "接入成本", "并网成本", "grid connection cost"),
    )

    return ParsedSiteSelectionRequest(
        request_id=_make_request_id(text),
        raw_text=text,
        project_family=project_family,
        project_type=project_type,
        project_type_zh=project_type_zh,
        region=_parse_region(text),
        capacity_mw=_parse_capacity_mw(text),
        exclude_water=exclude_water,
        max_slope_deg=_parse_slope_deg(text),
        exclude_protected_areas=exclude_protected,
        exclude_prime_farmland=exclude_prime_farmland,
        max_distance_to_substation_km=_parse_substation_distance_km(text),
        request_solar_irradiance=request_solar_irradiance,
        request_wind_resource=request_wind_resource,
        request_grid_connection_cost=request_grid_connection_cost,
        parser_mode="deterministic_rule_parser",
    )


def resolve_scope(parsed: ParsedSiteSelectionRequest) -> dict[str, Any]:
    in_scope_constraints: list[dict[str, Any]] = [
        {
            "constraint": "administrative_boundary",
            "status": "in_scope",
            "status_zh": "纳入当前请求范围",
            "reason_zh": "任何选址任务都必须先确定行政区边界。",
        }
    ]

    if parsed.exclude_water:
        in_scope_constraints.append(
            {
                "constraint": "exclude_water",
                "status": "in_scope",
                "status_zh": "纳入当前请求范围",
                "reason_zh": "用户明确要求避开或排除水体。",
            }
        )

    if parsed.max_slope_deg is not None:
        in_scope_constraints.append(
            {
                "constraint": "max_slope_deg",
                "status": "in_scope",
                "status_zh": "纳入当前请求范围",
                "threshold": parsed.max_slope_deg,
                "threshold_unit": "degree",
                "reason_zh": f"用户明确要求坡度小于或不超过 {parsed.max_slope_deg} 度。",
            }
        )

    if parsed.exclude_protected_areas:
        in_scope_constraints.append(
            {
                "constraint": "exclude_protected_areas",
                "status": "in_scope",
                "status_zh": "纳入当前请求范围",
                "reason_zh": "用户明确要求避开保护区。",
            }
        )

    requested_out_of_scope: list[dict[str, Any]] = []

    if parsed.exclude_prime_farmland:
        requested_out_of_scope.append(
            {
                "constraint": "exclude_prime_farmland",
                **OUT_OF_SCOPE_CONSTRAINTS["exclude_prime_farmland"],
            }
        )

    if parsed.max_distance_to_substation_km is not None:
        requested_out_of_scope.append(
            {
                "constraint": "max_distance_to_substation_km",
                "threshold": parsed.max_distance_to_substation_km,
                "threshold_unit": "km",
                **OUT_OF_SCOPE_CONSTRAINTS["max_distance_to_substation_km"],
            }
        )

    if parsed.request_solar_irradiance:
        requested_out_of_scope.append(
            {
                "constraint": "solar_irradiance",
                **OUT_OF_SCOPE_CONSTRAINTS["solar_irradiance"],
            }
        )

    if parsed.request_wind_resource:
        requested_out_of_scope.append(
            {
                "constraint": "wind_resource",
                **OUT_OF_SCOPE_CONSTRAINTS["wind_resource"],
            }
        )

    if parsed.request_grid_connection_cost:
        requested_out_of_scope.append(
            {
                "constraint": "grid_connection_cost",
                **OUT_OF_SCOPE_CONSTRAINTS["grid_connection_cost"],
            }
        )

    required_tool_ids: list[str] = []
    for item in in_scope_constraints:
        required_tool_ids.extend(CONSTRAINT_TOOLS.get(item["constraint"], []))

    required_tool_ids.extend(
        [
            "finalize_request_scoped_candidates",
            "build_map_preview",
            "validate_request_scoped_job",
        ]
    )

    unique_required_tool_ids = list(dict.fromkeys(required_tool_ids))

    scope_names = [
        item["constraint"]
        for item in in_scope_constraints
        if item["constraint"] != "administrative_boundary"
    ]
    if not scope_names:
        scope_kind = "boundary_only"
        scope_kind_zh = "仅行政边界范围"
    else:
        scope_kind = "+".join(scope_names)
        scope_kind_zh = " + ".join(scope_names)

    if parsed.project_type is None or parsed.region is None:
        request_status = "needs_clarification"
        request_status_zh = "需要补充项目类型或区域信息"
    elif requested_out_of_scope:
        request_status = "partially_supported"
        request_status_zh = "部分支持：存在当前版本暂不支持的约束"
    else:
        request_status = "supported"
        request_status_zh = "当前请求可支持"

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    return {
        "request_id": parsed.request_id,
        "created_at_utc": now,
        "request_status": request_status,
        "request_status_zh": request_status_zh,
        "scope_kind": scope_kind,
        "scope_kind_zh": scope_kind_zh,
        "parsed_request": asdict(parsed),
        "in_scope_constraints": in_scope_constraints,
        "requested_out_of_scope_constraints": requested_out_of_scope,
        "in_scope_constraint_count": len(in_scope_constraints),
        "requested_out_of_scope_constraint_count": len(requested_out_of_scope),
        "required_tool_ids": unique_required_tool_ids,
        "required_tool_count": len(unique_required_tool_ids),
        "next_stage": "create_dynamic_job_plan",
        "next_stage_zh": "下一步创建动态任务计划",
    }


def parse_and_resolve(raw_text: str) -> dict[str, Any]:
    parsed = parse_request_text(raw_text)
    return resolve_scope(parsed)

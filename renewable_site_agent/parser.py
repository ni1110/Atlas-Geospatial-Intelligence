"""Structured constraint parsing with Strands primary and safe fallback."""

from __future__ import annotations

import re

from pydantic import ValidationError

from renewable_site_agent.prompts import CONSTRAINT_PARSER_PROMPT, PROMPT_VERSION
from renewable_site_agent.schemas import AOI, SiteSelectionRequest

from .agent.routing import _subtype
from .agent.structured_llm import StrandsStructuredLLM


REGION_NAMES = (
    "北京", "北京市", "甘肃", "甘肃省", "四川", "四川省", "广东", "广东省",
    "上海", "天津", "重庆", "河北", "河南", "山东", "山西", "陕西", "青海",
    "云南", "贵州", "湖南", "湖北", "江西", "福建", "浙江", "江苏", "安徽",
    "辽宁", "吉林", "黑龙江", "海南", "内蒙古", "广西", "西藏", "宁夏", "新疆",
)


def _number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def deterministic_parse(text: str) -> SiteSelectionRequest:
    project_type = _subtype(text)
    region = next((name for name in REGION_NAMES if name in text), None)
    if region is None:
        match = re.search(
            r"(?:在|位于|in|region\s*[:=]?)\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z .'-]{1,30}?)(?:进行|建设|寻找|选址|做|的|,|，|$)",
            text,
            re.I,
        )
        if match:
            region = match.group(1).strip()
    bbox = None
    bbox_match = re.search(
        r"(?:bbox|AOI)\s*[:=]?\s*\[?\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if bbox_match:
        bbox = tuple(float(v) for v in bbox_match.groups())
    capacity = _number(text, (r"(\d+(?:\.\d+)?)\s*(?:MW|兆瓦)",))
    slope = _number(text, (r"(?:坡度|slope)[^\d]{0,8}(\d+(?:\.\d+)?)\s*(?:°|度)?",))
    substation = _number(
        text,
        (r"(?:变电站|substation)[^\d]{0,12}(\d+(?:\.\d+)?)\s*(?:km|公里)",),
    )
    road = _number(text, (r"(?:道路|公路|road)[^\d]{0,12}(\d+(?:\.\d+)?)\s*(?:km|公里)",))
    minimum_area = _number(text, (r"(?:连续面积|最小面积|minimum area)[^\d]{0,10}(\d+(?:\.\d+)?)\s*(?:km2|km²|平方公里)",))
    top_k_value = _number(text, (r"(?:top\s*|前)(\d+)",))
    payload = {
        "region": region,
        "aoi": AOI(bbox=bbox) if bbox else None,
        "project_type": project_type,
        "capacity_mw": capacity,
        "max_slope_deg": slope if slope is not None else 8.0,
        "exclude_water": not bool(re.search(r"不排除水体|include water", text, re.I)),
        "exclude_protected_area": not bool(re.search(r"不排除保护区|include protected", text, re.I)),
        "max_substation_distance_km": substation,
        "max_road_distance_km": road,
        "minimum_contiguous_area_km2": minimum_area,
        "top_k": int(top_k_value or 5),
        "require_remote_sensing": bool(re.search(r"遥感核验|remote sensing verification", text, re.I)),
        "require_vlm_verification": bool(re.search(r"VLM|视觉模型", text, re.I)),
    }
    return SiteSelectionRequest.model_validate(payload)


def parse_site_selection_request(
    user_request: str,
    *,
    llm: StrandsStructuredLLM | None = None,
    base_url: str | None = None,
    model_id: str | None = None,
) -> SiteSelectionRequest:
    """Parse with Strands structured output, then deterministic fallback.

    ``base_url`` and ``model_id`` remain accepted for source compatibility;
    model configuration is now centralized in ``config/model_policy.yaml``.
    """
    del base_url, model_id
    active_llm = llm if llm is not None else StrandsStructuredLLM.from_environment()
    if active_llm is not None:
        prompt = CONSTRAINT_PARSER_PROMPT.format(
            version=PROMPT_VERSION, query=user_request
        )
        for _ in range(2):
            try:
                return active_llm.generate(prompt, SiteSelectionRequest)
            except (ValidationError, ValueError, RuntimeError, TypeError):
                continue
    return deterministic_parse(user_request)


__all__ = ["deterministic_parse", "parse_site_selection_request"]

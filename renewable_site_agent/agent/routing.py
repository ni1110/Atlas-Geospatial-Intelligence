"""LLM-first intent routing with confidence and deterministic fallback gates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from renewable_site_agent.prompts import INTENT_ROUTER_PROMPT, PROMPT_VERSION
from renewable_site_agent.schemas import IntentDecision

from .structured_llm import StrandsStructuredLLM


SITE_TERMS = (
    "选址", "场址", "site selection", "siting", "候选地", "候选区域",
    "candidate area", "candidate site",
)
TERRAIN_TERMS = ("地形", "坡度", "高程", "dem", "terrain", "slope", "elevation")
REMOTE_TERMS = (
    "遥感", "卫星", "sentinel", "remote sensing", "satellite image", "影像",
)
DATA_TERMS = (
    "数据源", "哪里下载", "数据查询", "dataset", "data source", "stac",
    "worldcover", "jrc水体", "jrc water", "nasa power", "copernicus dem",
)
GIS_TERMS = (
    "gis", "缓冲区", "叠加", "投影", "裁剪", "buffer", "clip", "reproject",
    "拓扑", "合并", "geometry", "几何", "栅格转矢量", "矢量化", "intersect",
)
REGION_HINT = re.compile(
    r"(?:在|位于|region\s*[:=]?|in\s+)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z .'-]{1,30})",
    re.I,
)


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _subtype(text: str) -> str:
    lowered = text.lower()
    found = []
    if any(t in lowered for t in ("光伏", "太阳能", "solar", "pv")):
        found.append("solar")
    if any(t in lowered for t in ("风电", "风能", "wind")):
        found.append("wind")
    if any(t in lowered for t in ("储能", "storage", "battery")):
        found.append("storage")
    if "hybrid" in lowered or "混合" in lowered or len(found) > 1:
        return "hybrid"
    return found[0] if found else "unknown"


def deterministic_route(query: str) -> IntentDecision:
    text = query.strip()
    subtype = _subtype(text)
    # Specific analysis/data requests take precedence over the incidental word
    # "candidate" (for example, remote-sensing verification of candidates).
    if _contains(text, REMOTE_TERMS):
        return IntentDecision(
            intent="remote_sensing_analysis", subtype="unknown", confidence=0.86,
            selected_skill="remote-sensing", route_mode="deterministic_fallback",
        )
    if _contains(text, DATA_TERMS):
        return IntentDecision(
            intent="geospatial_data_query", subtype="unknown", confidence=0.84,
            selected_skill="general-gis", route_mode="deterministic_fallback",
        )
    if _contains(text, SITE_TERMS) or (
        subtype != "unknown" and _contains(text, ("建设", "候选", "项目", "farm", "plant"))
    ):
        intent, skill = "renewable_site_selection", "site-selection"
        missing = []
        if subtype == "unknown":
            missing.append("project_type")
        has_region = bool(REGION_HINT.search(text)) or bool(
            re.search(r"(北京|甘肃|四川|广东|省|市|自治区|municipality|province)", text, re.I)
        )
        if not has_region and not re.search(r"bbox|aoi|\d{2,3}\.\d+\s*,", text, re.I):
            missing.append("region_or_aoi")
        return IntentDecision(
            intent=intent,
            subtype=subtype,
            confidence=0.88 if not missing else 0.68,
            missing_fields=missing,
            needs_clarification=bool(missing),
            selected_skill=skill,
            route_mode="deterministic_fallback",
            reason="keyword fallback with required-field gate",
        )
    if _contains(text, TERRAIN_TERMS):
        return IntentDecision(
            intent="terrain_analysis", subtype="unknown", confidence=0.86,
            selected_skill="terrain-analysis", route_mode="deterministic_fallback",
        )
    if _contains(text, GIS_TERMS):
        return IntentDecision(
            intent="general_gis", subtype="unknown", confidence=0.8,
            selected_skill="general-gis", route_mode="deterministic_fallback",
        )
    return IntentDecision(
        intent="unknown", subtype="unknown", confidence=0.25,
        needs_clarification=True, selected_skill="none",
        route_mode="deterministic_fallback", reason="no supported intent matched",
    )


@dataclass
class IntentRouter:
    llm: StrandsStructuredLLM | None = None
    high_confidence: float = 0.8
    low_confidence: float = 0.55
    max_attempts: int = 2

    def route(self, query: str) -> IntentDecision:
        llm = self.llm if self.llm is not None else StrandsStructuredLLM.from_environment()
        if llm is not None:
            prompt = INTENT_ROUTER_PROMPT.format(version=PROMPT_VERSION, query=query)
            for _ in range(self.max_attempts):
                try:
                    decision = llm.generate(prompt, IntentDecision)
                    decision.route_mode = "llm"
                    return self._gate(decision)
                except Exception:
                    continue
        return self._gate(deterministic_route(query))

    def _gate(self, decision: IntentDecision) -> IntentDecision:
        if decision.confidence < self.low_confidence:
            decision.needs_clarification = True
        if decision.missing_fields:
            decision.needs_clarification = True
        if (
            decision.intent == "renewable_site_selection"
            and decision.subtype == "unknown"
            and "project_type" not in decision.missing_fields
        ):
            decision.missing_fields.append("project_type")
            decision.needs_clarification = True
        return decision

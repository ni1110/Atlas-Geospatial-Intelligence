"""Strictly separated legacy, deterministic, and live-LLM GeoAgent routing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from geoagent.orchestration.models import GeoGoalUnderstanding, GeoIntentDecision, GoalContract
from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM


ROLE_BY_INTENT: dict[str, list[str]] = {
    "general_gis": ["gis_analysis"],
    "data_discovery": ["data"],
    "terrain_analysis": ["data", "terrain"],
    "remote_sensing": ["data", "remote_sensing"],
    "renewable_siting": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"],
    "map_visualization": ["map_report"],
    "multi_domain": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"],
    "unknown": [],
}

TERMS = {
    "general_gis": ("buffer", "intersect", "clip", "dissolve", "spatial join", "zonal", "reproject", "缓冲", "相交", "裁剪", "融合", "空间连接"),
    "data_discovery": ("find dem", "dataset", "data source", "get roads", "download", "查找数据", "获取数据", "哪里有", "数据源", "worldcover", "nasa power"),
    "terrain_analysis": ("terrain", "elevation", "slope", "aspect", "hydrology", "dem", "地形", "高程", "坡度", "坡向", "水文"),
    "remote_sensing": ("sentinel", "stac search", "remote sensing", "satellite", "ndvi", "rgb chip", "遥感", "卫星", "影像", "植被指数"),
    "renewable_siting": ("site selection", "siting", "solar farm", "wind farm", "storage site", "光伏选址", "风电选址", "储能选址", "候选场址"),
    "map_visualization": ("add to map", "show on map", "visualize map", "制图", "添加到地图", "地图展示", "可视化"),
}

# Distance/proximity requests are ordinary GIS operations. Keep these terms
# explicit so the deterministic router can select the narrowly scoped GIS
# agent without falling through to ``unknown``.
TERMS["general_gis"] += ("distance", "proximity", "距离", "邻近")
TERMS["general_gis"] += ("road", "accessibility", "道路", "可达性", "交通")
TERMS["map_visualization"] += ("report", "summary", "报告", "摘要")

ROLE_ORDER = (
    "data", "terrain", "gis_analysis", "remote_sensing",
    "renewable_siting", "reviewer", "map_report",
)


def _union_roles(intents: set[str], *, include_reviewer: bool = False) -> list[str]:
    roles = {
        role
        for intent in intents
        for role in ROLE_BY_INTENT.get(intent, [])
        if role not in {"orchestrator"}
    }
    if include_reviewer:
        roles.add("reviewer")
    return [role for role in ROLE_ORDER if role in roles]


def _has(text: str, intent: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in TERMS[intent])


def _subtype(text: str) -> str:
    lowered = text.lower()
    hits = []
    if any(term in lowered for term in ("solar", "pv", "光伏", "太阳能")):
        hits.append("solar")
    if any(term in lowered for term in ("wind", "风电", "风能")):
        hits.append("wind")
    if any(term in lowered for term in ("storage", "battery", "储能")):
        hits.append("storage")
    return "hybrid" if len(set(hits)) > 1 or "hybrid" in lowered or "混合" in lowered else (hits[0] if hits else "unknown")


def legacy_keyword_route(query: str) -> GeoIntentDecision:
    """Frozen narrow baseline; never used as the production fallback."""
    text = query.lower()
    if "dem" in text or "slope" in text or "坡度" in text:
        intent = "terrain_analysis"
    elif "sentinel" in text or "remote sensing" in text:
        intent = "remote_sensing"
    elif "site selection" in text or "光伏选址" in text:
        intent = "renewable_siting"
    elif "gis" in text or "buffer" in text:
        intent = "general_gis"
    else:
        intent = "unknown"
    return GeoIntentDecision(
        intent=intent,
        subtype=_subtype(query),
        confidence=0.55 if intent != "unknown" else 0.2,
        required_agent_roles=ROLE_BY_INTENT[intent],
        needs_clarification=intent == "unknown",
        route_mode="legacy_keyword",
        reason="frozen keyword baseline",
    )


def deterministic_route(query: str) -> GeoIntentDecision:
    text = query.strip()
    hits = [name for name in TERMS if _has(text, name)]
    comparative = any(term in text.lower() for term in ("compare", "comparison", "versus", " vs ", "比较", "对比", "哪个地区"))
    region_count = len(set(re.findall(r"甘肃|青海|北京|四川|gansu|qinghai|beijing|sichuan", text, re.I)))
    discovery_verb = any(term in text.lower() for term in ("find", "search for", "locate", "找", "查找", "获取"))
    domain_hits = {item for item in hits if item not in {"map_visualization"}}
    if (comparative and (len(domain_hits) >= 2 or region_count >= 2 or _subtype(text) != "unknown")) or len(domain_hits) >= 2:
        intent = "multi_domain"
    elif "renewable_siting" in hits:
        intent = "renewable_siting"
    elif "remote_sensing" in hits:
        intent = "remote_sensing"
    elif "data_discovery" in hits or (discovery_verb and any(name in hits for name in ("terrain_analysis", "remote_sensing"))):
        intent = "data_discovery"
    elif "terrain_analysis" in hits:
        intent = "terrain_analysis"
    elif "general_gis" in hits:
        intent = "general_gis"
    elif "map_visualization" in hits:
        intent = "map_visualization"
    else:
        intent = "unknown"
    subtype = _subtype(text)
    missing: list[str] = []
    if intent == "renewable_siting" and subtype == "unknown":
        missing.append("project_type")
    if intent in {"renewable_siting", "multi_domain", "terrain_analysis", "remote_sensing", "data_discovery"}:
        has_location = bool(re.search(r"\b(?:gansu|qinghai|beijing|sichuan|aoi|bbox)\b|甘肃|青海|北京|四川|\d{2,3}\.\d+\s*,", text, re.I))
        if not has_location:
            missing.append("region_or_aoi")
    if intent == "unknown":
        missing.append("supported_goal")
    required_roles = (
        _union_roles(set(hits), include_reviewer=True)
        if intent == "multi_domain"
        else ROLE_BY_INTENT[intent]
    )
    return GeoIntentDecision(
        intent=intent,
        subtype=subtype,
        confidence=0.9 if intent != "unknown" and not missing else (0.68 if intent != "unknown" else 0.2),
        required_agent_roles=required_roles,
        missing_fields=missing,
        needs_clarification=bool(missing),
        route_mode="deterministic",
        reason=f"matched categories={hits}",
    )


@dataclass
class HierarchicalIntentRouter:
    llm: StrandsStructuredLLM | None = None
    max_attempts: int = 2
    last_llm_call_count: int = field(default=0, init=False)
    last_llm_provider: str | None = field(default=None, init=False)
    last_llm_model: str | None = field(default=None, init=False)
    last_llm_error: str | None = field(default=None, init=False)

    def _active_llm(self, use_llm: bool) -> StrandsStructuredLLM | None:
        active = self.llm if self.llm is not None else (StrandsStructuredLLM.from_environment() if use_llm else None)
        if use_llm and active is None:
            self.last_llm_error = StrandsStructuredLLM.last_environment_error
        return active

    def route(self, query: str, *, use_llm: bool = True) -> GeoIntentDecision:
        active = self._active_llm(use_llm)
        if active is not None:
            prompt = (
                "Classify this geospatial goal. Return only the requested structured model. "
                "Intents: general_gis, data_discovery, terrain_analysis, remote_sensing, "
                "renewable_siting, map_visualization, multi_domain, unknown. Multi-domain "
                "requires multiple specialist roles. Never silently invent missing AOI or project type.\n"
                f"USER GOAL:\n{query}"
            )
            for _ in range(self.max_attempts):
                try:
                    decision = active.generate(prompt, GeoIntentDecision)
                    self._record_llm(active)
                    decision.route_mode = "llm"
                    return self._merge_explicit_domains(query, self._gate(decision))
                except Exception as exc:
                    self.last_llm_error = f"{type(exc).__name__}: {exc}"[:2000]
                    self._record_llm(active)
                    continue
        return self._gate(deterministic_route(query))

    def understand(self, query: str, *, use_llm: bool = True) -> tuple[GeoIntentDecision, GoalContract]:
        """LLM-first joint Goal/Intent understanding with deterministic fallback."""
        active = self._active_llm(use_llm)
        if active is not None:
            prompt = (
                "You are the GeoAgent goal and intent decision layer. Return only the requested "
                "structured model. Preserve every explicit user hard constraint; never invent an "
                "AOI, dataset, threshold, project subtype, or output. Classify intent as one of "
                "general_gis, data_discovery, terrain_analysis, remote_sensing, renewable_siting, "
                "map_visualization, multi_domain, unknown. Set route_mode to llm, enumerate the "
                "minimum required specialist roles, and use missing_fields/needs_clarification "
                "when essential information is absent. Normalize the goal without changing it.\n"
                f"USER GOAL:\n{query}"
            )
            for _ in range(self.max_attempts):
                try:
                    understanding = active.generate(prompt, GeoGoalUnderstanding)
                    self._record_llm(active)
                    understanding.decision.route_mode = "llm"
                    decision = self._merge_explicit_domains(query, self._gate(understanding.decision))
                    understanding.decision = decision
                    contract = understanding.to_contract(query)
                    if contract.intent != decision.intent:
                        raise ValueError("LLM goal and intent disagree")
                    return decision, contract
                except Exception as exc:
                    self.last_llm_error = f"{type(exc).__name__}: {exc}"[:2000]
                    self._record_llm(active)
                    continue
        decision = self._gate(deterministic_route(query))
        return decision, parse_goal_contract(query, decision)

    def _record_llm(self, active: StrandsStructuredLLM) -> None:
        self.last_llm_call_count = int(getattr(active, "call_count", 0))
        self.last_llm_provider = str(getattr(active, "provider", "unknown"))
        self.last_llm_model = getattr(active, "model_id", None)

    @staticmethod
    def _merge_explicit_domains(query: str, decision: GeoIntentDecision) -> GeoIntentDecision:
        """Govern an LLM decision so explicit capability domains cannot disappear."""
        baseline = deterministic_route(query)
        if baseline.intent != "multi_domain":
            return decision
        merged_roles = set(decision.required_agent_roles) | set(baseline.required_agent_roles) | {"reviewer"}
        roles = [role for role in ROLE_ORDER if role in merged_roles]
        return decision.model_copy(update={
            "intent": "multi_domain",
            "required_agent_roles": roles,
            "reason": f"{decision.reason}; explicit-domain-union={baseline.reason}",
        })

    @staticmethod
    def _gate(decision: GeoIntentDecision) -> GeoIntentDecision:
        required = ROLE_BY_INTENT[decision.intent]
        if not decision.required_agent_roles:
            decision.required_agent_roles = required  # type: ignore[assignment]
        if decision.missing_fields or decision.confidence < 0.55:
            decision.needs_clarification = True
        return decision


def parse_goal_contract(query: str, decision: GeoIntentDecision) -> GoalContract:
    regions = re.findall(r"甘肃|青海|北京|四川|Gansu|Qinghai|Beijing|Sichuan", query, re.I)
    bbox = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,2}(?:\.\d+)?)", query)
    region_or_aoi: dict[str, Any] | str | None = None
    if bbox:
        region_or_aoi = {"bbox": [float(item) for item in bbox.groups()], "crs": "EPSG:4326"}
    elif regions:
        region_or_aoi = {"regions": list(dict.fromkeys(regions))}
    hard: dict[str, Any] = {}
    buffer_match = re.search(r"(\d+(?:\.\d+)?)\s*(m|meter|meters|米)\s*(?:buffer|缓冲)", query, re.I)
    if buffer_match:
        hard["buffer_distance_m"] = float(buffer_match.group(1))
    if "排除水体" in query or "exclude water" in query.lower():
        hard["exclude_water"] = True
    if "最大坡度" in query:
        slope = re.search(r"最大坡度\s*(\d+(?:\.\d+)?)", query)
        if slope:
            hard["max_slope_deg"] = float(slope.group(1))
    outputs = ["structured_result"]
    if _has(query, "map_visualization") or decision.intent == "map_visualization":
        outputs.append("map")
    return GoalContract(
        primary_goal=query,
        intent=decision.intent,
        subtype=decision.subtype,
        region_or_aoi=region_or_aoi,
        hard_constraints=hard,
        explicit_user_requirements=[query],
        missing_fields=tuple(decision.missing_fields),
        expected_outputs=tuple(outputs),
        allowed_degradation=("provider_fallback_with_provenance",),
    )

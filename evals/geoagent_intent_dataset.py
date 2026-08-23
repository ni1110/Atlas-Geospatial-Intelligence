"""Human-labeled GeoAgent intent data; held-out cases are frozen."""

from __future__ import annotations


def _labeled(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Attach the human-reviewed missing-field label to every case."""
    for row in rows:
        missing: list[str] = []
        if bool(row["clarify"]):
            if row["intent"] == "unknown":
                missing = ["supported_goal"]
            else:
                if row["intent"] == "renewable_siting" and row["subtype"] == "unknown":
                    missing.append("project_type")
                missing.append("region_or_aoi")
        row["missing_fields"] = missing
    return rows


def regression_cases() -> list[dict[str, object]]:
    return _labeled([
        {"query": "给道路做500m buffer", "intent": "general_gis", "subtype": "unknown", "agents": ["gis_analysis"], "clarify": False},
        {"query": "Buffer the roads by 500 meters and intersect construction land", "intent": "general_gis", "subtype": "unknown", "agents": ["gis_analysis"], "clarify": False},
        {"query": "裁剪这个矢量图层并做 dissolve", "intent": "general_gis", "subtype": "unknown", "agents": ["gis_analysis"], "clarify": False},
        {"query": "spatial join these parcels with counties", "intent": "general_gis", "subtype": "unknown", "agents": ["gis_analysis"], "clarify": False},
        {"query": "找甘肃 DEM 数据", "intent": "data_discovery", "subtype": "unknown", "agents": ["data"], "clarify": False},
        {"query": "获取甘肃道路和变电站数据", "intent": "data_discovery", "subtype": "unknown", "agents": ["data"], "clarify": False},
        {"query": "Find land-cover data for Gansu", "intent": "data_discovery", "subtype": "unknown", "agents": ["data"], "clarify": False},
        {"query": "查询青海 NASA POWER 数据源", "intent": "data_discovery", "subtype": "unknown", "agents": ["data"], "clarify": False},
        {"query": "分析甘肃坡度", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": False},
        {"query": "Calculate elevation and aspect in Qinghai", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": False},
        {"query": "甘肃基础地形和水文分析", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": False},
        {"query": "terrain statistics for bbox 102.9,37.5,103.1,37.7", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": False},
        {"query": "搜索甘肃 Sentinel 影像并计算 NDVI", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": False},
        {"query": "Find a cloud-free Sentinel-2 RGB chip in Qinghai", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": False},
        {"query": "遥感分析甘肃地表覆盖", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": False},
        {"query": "STAC search and band selection for this bbox 102.9,37.5,103.1,37.7", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": False},
        {"query": "帮我做甘肃光伏选址", "intent": "renewable_siting", "subtype": "solar", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": False},
        {"query": "Wind farm site selection in Qinghai", "intent": "renewable_siting", "subtype": "wind", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": False},
        {"query": "甘肃储能选址", "intent": "renewable_siting", "subtype": "storage", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": False},
        {"query": "Hybrid renewable siting in Gansu", "intent": "renewable_siting", "subtype": "hybrid", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": False},
        {"query": "把结果添加到地图", "intent": "map_visualization", "subtype": "unknown", "agents": ["map_report"], "clarify": False},
        {"query": "Render this layer on the map", "intent": "map_visualization", "subtype": "unknown", "agents": ["map_report"], "clarify": False},
        {"query": "比较甘肃和青海的地形、太阳能资源和基础设施，哪个更适合光伏", "intent": "multi_domain", "subtype": "solar", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"], "clarify": False},
        {"query": "Compare Gansu and Qinghai terrain, solar resource and roads for PV", "intent": "multi_domain", "subtype": "solar", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"], "clarify": False},
        {"query": "帮我做新能源选址", "intent": "renewable_siting", "subtype": "unknown", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": True},
        {"query": "分析坡度", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": True},
        {"query": "搜索 Sentinel 影像", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": True},
        {"query": "hello", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
        {"query": "写一首诗", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
        {"query": "What is the capital of France?", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
    ])


def held_out_cases() -> list[dict[str, object]]:
    return _labeled([
        {"query": "道路外扩一公里，再和工业用地取交集", "intent": "general_gis", "subtype": "unknown", "agents": ["gis_analysis"], "clarify": False},
        {"query": "Where can I get WorldCover for Qinghai?", "intent": "data_discovery", "subtype": "unknown", "agents": ["data"], "clarify": False},
        {"query": "看看甘肃地势起伏和坡向", "intent": "terrain_analysis", "subtype": "unknown", "agents": ["data", "terrain"], "clarify": False},
        {"query": "青海卫星图算一下植被指数", "intent": "remote_sensing", "subtype": "unknown", "agents": ["data", "remote_sensing"], "clarify": False},
        {"query": "Gansu solar site screening", "intent": "renewable_siting", "subtype": "solar", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer"], "clarify": False},
        {"query": "把候选地画出来", "intent": "map_visualization", "subtype": "unknown", "agents": ["map_report"], "clarify": False},
        {"query": "对比甘肃、青海的坡度、风资源和电网，判断风电潜力", "intent": "multi_domain", "subtype": "wind", "agents": ["data", "terrain", "gis_analysis", "renewable_siting", "reviewer", "map_report"], "clarify": False},
        {"query": "Do something with maps", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
        {"query": "哪里适合建场站", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
        {"query": "给我查数据", "intent": "unknown", "subtype": "unknown", "agents": [], "clarify": True},
    ])

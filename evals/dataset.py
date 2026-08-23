"""Labeled multilingual intent dataset (generated deterministically, 110 cases)."""

from __future__ import annotations

from typing import Any


def routing_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    regions = ["北京", "甘肃", "四川", "广东"]
    projects = [
        ("光伏", "solar"), ("风电", "wind"), ("储能", "storage"), ("风光储混合", "hybrid")
    ]
    site_templates = [
        "在{region}为100MW{project}项目筛选候选地",
        "请做{region}{project}场站选址并输出Top 5",
        "Find candidate areas for a {subtype} plant in {region}",
    ]
    for region in regions:
        for project, subtype in projects:
            for template in site_templates:
                cases.append(
                    {
                        "query": template.format(region=region, project=project, subtype=subtype),
                        "intent": "renewable_site_selection", "subtype": subtype,
                        "skill": "site-selection", "clarification": False,
                    }
                )
    for query in [
        "计算北京的坡度和高程统计", "用DEM分析甘肃地形", "四川坡向坡度分析",
        "Terrain and slope analysis in Guangdong", "clip a DEM to this AOI", "生成等高线并汇总地形",
        "检查栅格投影后计算坡度", "elevation statistics for this region", "地形起伏度分析",
        "reproject and clip elevation raster", "坡度超过15度的区域", "DEM 数据质量检查",
    ]:
        cases.append({"query": query, "intent": "terrain_analysis", "subtype": "unknown", "skill": "terrain-analysis", "clarification": False})
    for query in [
        "搜索最新Sentinel-2卫星影像", "对候选地做遥感核验", "分析这片区域的遥感影像",
        "Remote sensing analysis for this AOI", "Sentinel cloud-free image search", "用卫星判断地表覆盖",
        "获取Sentinel影像chip", "satellite image verification", "遥感变化检测", "分析多光谱影像",
        "查询云量低于10%的卫星影像", "remote sensing evidence for candidates",
    ]:
        cases.append({"query": query, "intent": "remote_sensing_analysis", "subtype": "unknown", "skill": "remote-sensing", "clarification": False})
    for query in [
        "给道路做5公里缓冲区", "把这个图层重投影到UTM", "裁剪并叠加两个GIS图层",
        "Run a GIS buffer operation", "clip this vector layer", "检查几何拓扑错误",
        "合并相邻面要素", "calculate nearest distance in GIS", "修复无效geometry", "栅格转矢量",
        "GIS空间相交分析", "reproject this GeoJSON",
    ]:
        cases.append({"query": query, "intent": "general_gis", "subtype": "unknown", "skill": "general-gis", "clarification": False})
    for query in [
        "哪里可以查询WorldCover数据源", "查询Copernicus DEM dataset", "有哪些地理数据源",
        "Find a STAC dataset for land cover", "JRC水体数据查询", "查询NASA POWER数据",
        "geospatial data source for roads", "从哪里下载行政边界数据",
    ]:
        cases.append({"query": query, "intent": "geospatial_data_query", "subtype": "unknown", "skill": "general-gis", "clarification": False})
    for query in [
        "今天天气怎么样", "写一首诗", "帮我整理会议纪要", "What is the capital of France?",
        "解释量子计算", "推荐一部电影", "hello", "计算1+1",
    ]:
        cases.append({"query": query, "intent": "unknown", "subtype": "unknown", "skill": "none", "clarification": True})
    for query, subtype in [
        ("帮我做新能源选址", "unknown"), ("选择一块光伏候选地", "solar"),
        ("在甘肃做场站选址", "unknown"), ("wind site selection please", "wind"),
        ("在四川寻找新能源候选区域", "unknown"), ("储能项目选址", "storage"),
        ("广东新能源场站候选地", "unknown"), ("solar plant candidate area", "solar"),
        ("请筛选场址", "unknown"), ("北京建设项目选址", "unknown"),
    ]:
        cases.append({"query": query, "intent": "renewable_site_selection", "subtype": subtype, "skill": "site-selection", "clarification": True})
    assert len(cases) == 110
    return cases

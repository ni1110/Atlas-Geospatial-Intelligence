from __future__ import annotations

import pytest

from renewable_site_agent.mcp.client import SkillScopedMCPClient


pytestmark = pytest.mark.integration


def test_strands_mcpclient_discovers_scoped_tools_and_calls_real_gis() -> None:
    suitable = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[116, 39], [116.02, 39], [116.02, 39.02], [116, 39.02], [116, 39]]]}}
        ],
    }
    with SkillScopedMCPClient("site-selection") as client:
        names = client.list_tool_names()
        assert "build_candidate_polygons" in names
        result = client.call(
            "build_candidate_polygons",
            {"suitable_geojson": suitable, "minimum_area_km2": 0.01},
        )
    assert result.status == "success"
    assert result.metadata["candidate_count"] == 1


def test_mcp_skill_filter_rejects_out_of_scope_tool() -> None:
    with SkillScopedMCPClient("terrain-analysis") as client:
        names = client.list_tool_names()
        assert "acquire_dem" in names
        assert "acquire_power_infrastructure" not in names
        result = client.call("acquire_power_infrastructure", {"bbox": [116, 39, 116.1, 39.1]})
    assert result.status == "failed"

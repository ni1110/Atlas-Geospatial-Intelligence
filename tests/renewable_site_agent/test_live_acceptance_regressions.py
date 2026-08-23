from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from renewable_site_agent.data.cache import DataCache
from renewable_site_agent.data.manager import DataSourceManager
from renewable_site_agent.data import providers
from renewable_site_agent.schemas import DataArtifact, ToolResult


def test_gansu_chinese_alias_is_unicode_safe() -> None:
    assert providers._normalized_region("\u7518\u8083") == "gansu"
    assert providers._normalized_region("\u7518\u8083\u7701") == "gansu"


def test_copernicus_stac_normalizes_date_only_interval(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_http_json(url: str, **kwargs: Any) -> tuple[dict[str, Any], int, float]:
        captured.update({"url": url, **kwargs})
        return {
            "features": [{
                "id": "live-format-regression",
                "assets": {"B04": {"href": "s3://example/B04.jp2"}},
            }]
        }, 123, 4.0

    monkeypatch.setattr(providers, "http_json", fake_http_json)
    result = providers.SentinelSTACProvider().fetch(
        {
            "bbox": [102.99, 37.59, 103.09, 37.69],
            "date_range": "2025-05-01/2025-10-31",
            "cloud_cover": 30,
        }
    )

    assert result.status == "success"
    assert captured["url"] == "https://stac.dataspace.copernicus.eu/v1/search"
    assert captured["payload"]["datetime"] == "2025-05-01T00:00:00Z/2025-10-31T23:59:59Z"


@dataclass
class _MaterializedProvider:
    materialized_path: Path
    name: str = "materialized_stub"
    dataset: str = "dem"
    version: str = "test"

    def fetch(self, query: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(
            artifact=DataArtifact(
                provider=self.name,
                dataset=self.dataset,
                dataset_version=self.version,
                source_url="https://example.invalid/window.tif",
                bbox=tuple(query["bbox"]),
                crs="EPSG:32648",
                resolution="30 m",
                license="test",
                attribution="test",
                local_path=str(self.materialized_path),
            )
        )


def test_cache_envelope_does_not_replace_materialized_path(tmp_path: Path) -> None:
    materialized = tmp_path / "window.tif"
    materialized.write_bytes(b"small-raster-placeholder")
    manager = DataSourceManager(cache=DataCache(root=tmp_path / "cache"))
    manager.config = {"dem": {"priority": ["materialized_stub"]}}
    manager.providers = {"materialized_stub": _MaterializedProvider(materialized)}
    query = {"bbox": [102.99, 37.59, 103.09, 37.69]}

    first = manager.acquire("dem", query)
    second = manager.acquire("dem", query)

    assert first.artifact.local_path == str(materialized)
    assert second.metrics["cache_hit"] is True
    assert second.artifact.local_path == str(materialized)

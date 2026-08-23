from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from renewable_site_agent.agent.skills import SkillRegistry
from renewable_site_agent.durable.store import DurableJobStore
from renewable_site_agent.durable.worker import CanonicalWorker, WorkerCrash
from renewable_site_agent.gis.engine import GISExecutor
from renewable_site_agent.schemas import DataArtifact, ToolResult
from renewable_site_agent.service import CanonicalSiteSelectionService


pytestmark = [pytest.mark.offline, pytest.mark.integration]


REGIONS = {
    "北京": (116.20, 39.80, 116.30, 39.90),
    "甘肃": (103.70, 36.00, 103.80, 36.10),
    "四川": (104.00, 30.60, 104.10, 30.70),
    "广东": (113.20, 23.00, 113.30, 23.10),
}


class FixtureMCP:
    """Small real GIS fixtures; only external provider I/O is replaced."""

    def __init__(self, root: Path, skill: str = "site-selection") -> None:
        self.root = root
        self.skill = skill
        self.calls: Counter[str] = Counter()
        self.gis = GISExecutor()
        self.current_bbox = REGIONS["北京"]

    def __enter__(self) -> "FixtureMCP":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def list_tool_names(self) -> list[str]:
        return list(SkillRegistry().allowed_tools(self.skill))

    @staticmethod
    def _artifact(dataset: str, *, metadata: dict[str, Any], local_path: str | None = None, crs: str = "EPSG:4326") -> DataArtifact:
        return DataArtifact(
            provider="test_fixture", dataset=dataset, dataset_version="1",
            source_url="fixture://offline", crs=crs, resolution="small fixture",
            license="test-only", attribution="generated test fixture",
            local_path=local_path, metadata=metadata,
        )

    def _boundary(self, region: str) -> ToolResult:
        match = next((value for key, value in REGIONS.items() if key in region), None)
        if match is None:
            raise ValueError(region)
        self.current_bbox = match
        west, south, east, north = match
        collection = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {"region": region},
                "geometry": {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]},
            }],
        }
        artifact = self._artifact("administrative_boundary", metadata={"feature_collection": collection})
        return ToolResult.ok(artifact=artifact, provenance=[artifact])

    def _polygon_overpass(self, dataset: str, bbox: list[float], offset: float) -> ToolResult:
        west, south, east, north = bbox
        dx, dy = east - west, north - south
        x0, y0 = west + dx * offset, south + dy * offset
        x1, y1 = x0 + dx * 0.08, y0 + dy * 0.08
        points = [
            {"lon": x0, "lat": y0}, {"lon": x1, "lat": y0},
            {"lon": x1, "lat": y1}, {"lon": x0, "lat": y1}, {"lon": x0, "lat": y0},
        ]
        data = {"elements": [{"type": "way", "geometry": points, "tags": {"dataset": dataset}}]}
        artifact = self._artifact(dataset, metadata={"overpass_json": data})
        return ToolResult.ok(artifact=artifact, provenance=[artifact])

    def _dem(self, bbox: list[float]) -> ToolResult:
        import rasterio
        from pyproj import Transformer
        from rasterio.transform import from_bounds

        west, south, east, north = bbox
        zone = int(((west + east) / 2 + 180) // 6) + 1
        crs = f"EPSG:{32600 + zone}"
        transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x0, y0 = transformer.transform(west, south)
        x1, y1 = transformer.transform(east, north)
        path = self.root / f"dem-{abs(hash(tuple(bbox)))}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = np.tile(np.linspace(100, 101, 80, dtype="float32"), (80, 1))
        with rasterio.open(
            path, "w", driver="GTiff", width=80, height=80, count=1,
            dtype="float32", crs=crs, transform=from_bounds(x0, y0, x1, y1, 80, 80),
            nodata=-9999,
        ) as target:
            target.write(data, 1)
        artifact = self._artifact("dem", metadata={}, local_path=str(path), crs=crs)
        return ToolResult.ok(artifact=artifact, provenance=[artifact])

    def _land(self, bbox: list[float]) -> ToolResult:
        import rasterio
        from rasterio.transform import from_bounds

        path = self.root / f"land-{abs(hash(tuple(bbox)))}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = np.full((80, 80), 30, dtype="uint8")
        with rasterio.open(
            path, "w", driver="GTiff", width=80, height=80, count=1,
            dtype="uint8", crs="EPSG:4326", transform=from_bounds(*bbox, 80, 80), nodata=255,
        ) as target:
            target.write(data, 1)
        artifact = self._artifact("land_cover", metadata={}, local_path=str(path))
        return ToolResult.ok(artifact=artifact, provenance=[artifact])

    def call(self, name: str, arguments: dict[str, Any], timeout_seconds: float = 60) -> ToolResult:
        del timeout_seconds
        self.calls[name] += 1
        if name == "resolve_administrative_boundary":
            return self._boundary(arguments["region"])
        if name == "acquire_water_mask":
            return self._polygon_overpass("water", arguments["bbox"], 0.10)
        if name == "acquire_protected_areas":
            return self._polygon_overpass("protected_areas", arguments["bbox"], 0.80)
        if name == "acquire_dem":
            return self._dem(arguments["bbox"])
        if name == "acquire_land_cover":
            return self._land(arguments["bbox"])
        if name == "acquire_power_infrastructure":
            west, south, east, north = arguments["bbox"]
            data = {"elements": [{"type": "node", "lon": west + (east - west) * 0.5, "lat": south + (north - south) * 0.5, "tags": {"power": "substation"}}]}
            artifact = self._artifact("power_infrastructure", metadata={"overpass_json": data})
            return ToolResult.ok(artifact=artifact, provenance=[artifact])
        if name == "acquire_road_network":
            west, south, east, north = arguments["bbox"]
            data = {"elements": [{"type": "way", "geometry": [{"lon": west, "lat": south + (north - south) * 0.7}, {"lon": east, "lat": south + (north - south) * 0.7}], "tags": {"highway": "primary"}}]}
            artifact = self._artifact("roads", metadata={"overpass_json": data})
            return ToolResult.ok(artifact=artifact, provenance=[artifact])
        if name == "acquire_energy_resource":
            parameter = "ALLSKY_SFC_SW_DWN" if arguments["project_type"] == "solar" else "WS50M"
            artifact = self._artifact("energy_resource", metadata={"parameter": parameter, "values": {"ANN": 5.0}})
            return ToolResult.ok(artifact=artifact, provenance=[artifact])
        if name == "calculate_slope":
            return self.gis.calculate_slope(**arguments)
        if name == "apply_spatial_exclusion":
            return self.gis.apply_spatial_exclusion(**arguments)
        if name == "build_candidate_polygons":
            return self.gis.build_candidate_polygons(**arguments)
        if name == "calculate_nearest_distance":
            return self.gis.calculate_nearest_distance(**arguments)
        if name == "calculate_candidate_statistics":
            return self.gis.calculate_candidate_statistics(**arguments)
        if name == "validate_candidate_geometry":
            return self.gis.validate_candidate_geometry(**arguments)
        raise KeyError(name)


@pytest.fixture
def durable(tmp_path: Path) -> tuple[DurableJobStore, FixtureMCP]:
    return DurableJobStore(tmp_path / "jobs.sqlite3"), FixtureMCP(tmp_path / "fixtures")


def _factory(client: FixtureMCP):
    def build(skill: str) -> FixtureMCP:
        client.skill = skill
        return client
    return build


def test_queue_worker_checkpoint_result_and_four_region_generalization(
    durable: tuple[DurableJobStore, FixtureMCP],
) -> None:
    store, client = durable
    service = CanonicalSiteSelectionService(store=store)
    job_ids = []
    for region in REGIONS:
        job, _ = service.submit(f"在{region}建设100MW光伏电站并进行选址，最大坡度8度，Top 2")
        job_ids.append(job["job_id"])
    worker = CanonicalWorker(store=store, mcp_factory=_factory(client))
    worker.run_forever(stop_after_idle=True)
    for job_id in job_ids:
        job = store.get_job(job_id)
        assert job["status"] == "SUCCEEDED"
        assert job["result"]["top_k"]
        assert Path(job["result"]["result_path"]).is_file()
        assert Path(job["result"]["map_data_path"]).is_file()
        assert len(store.steps(job_id)) == job["total_steps"]


def test_worker_crash_resume_skips_completed_steps(
    durable: tuple[DurableJobStore, FixtureMCP],
) -> None:
    store, client = durable
    job, _ = CanonicalSiteSelectionService(store=store).submit(
        "在甘肃建设100MW光伏电站并进行选址，最大坡度8度，Top 2",
        idempotency_key="crash-resume",
    )
    crashing = CanonicalWorker(
        store=store, mcp_factory=_factory(client), crash_after_step="acquire_water_mask"
    )
    crashing.lease_seconds = 0
    with pytest.raises(WorkerCrash):
        crashing.run_once()
    assert client.calls["resolve_administrative_boundary"] == 1
    assert client.calls["acquire_water_mask"] == 1

    resumed = CanonicalWorker(store=store, mcp_factory=_factory(client))
    resumed.lease_seconds = 0
    result = resumed.run_once()
    assert result is not None and result["status"] == "SUCCEEDED"
    assert client.calls["resolve_administrative_boundary"] == 1
    assert client.calls["acquire_water_mask"] == 1
    events = store.events(job["job_id"])
    assert any(event["event_type"] == "worker_resumed" for event in events)
    assert sum(event["event_type"] == "idempotency_hit" for event in events) >= 2

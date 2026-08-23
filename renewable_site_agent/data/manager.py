"""Provider registry, cache reuse, fallback provenance, and normalization."""

from __future__ import annotations

import json
import time
from typing import Any

from renewable_site_agent.config import load_config
from renewable_site_agent.schemas import DataArtifact, ErrorCode, ToolResult

from .cache import DataCache
from .providers import (
    CopernicusDEMProvider,
    GeoBoundariesProvider,
    JRCSurfaceWaterProvider,
    LocalGeoBoundariesProvider,
    NASAPowerProvider,
    OSMOverpassProvider,
    PlanetaryComputerSTACProvider,
    SentinelSTACProvider,
    WorldCoverProvider,
)


class DataSourceManager:
    def __init__(self, cache: DataCache | None = None) -> None:
        self.cache = cache or DataCache()
        self.config = load_config("data_sources.yaml")
        self.providers = {
            "geoboundaries": GeoBoundariesProvider(),
            "local_geoboundaries": LocalGeoBoundariesProvider(),
            "copernicus_glo30": CopernicusDEMProvider(),
            "jrc_gsw": JRCSurfaceWaterProvider(),
            "esa_worldcover": WorldCoverProvider(),
            "nasa_power": NASAPowerProvider(),
            "copernicus_dataspace_stac": SentinelSTACProvider(),
            "planetary_computer_stac": PlanetaryComputerSTACProvider(),
            "osm_water": OSMOverpassProvider(dataset="water"),
            "osm_protected": OSMOverpassProvider(dataset="protected_areas"),
            "osm_power": OSMOverpassProvider(dataset="power_infrastructure"),
            "osm_roads": OSMOverpassProvider(dataset="roads"),
        }
        self.dataset_aliases = {
            "water": {"osm_overpass": "osm_water"},
            "protected_areas": {"local_wdpa": "local_wdpa", "osm_overpass": "osm_protected"},
            "power_infrastructure": {"osm_overpass": "osm_power"},
            "roads": {"osm_overpass": "osm_roads"},
        }

    def _provider_names(self, dataset: str) -> list[str]:
        section = self.config.get(dataset, {})
        return list(section.get("priority", []))

    def _resolve_name(self, dataset: str, configured: str) -> str:
        return self.dataset_aliases.get(dataset, {}).get(configured, configured)

    def acquire(self, dataset: str, query: dict[str, Any]) -> ToolResult:
        names = self._provider_names(dataset)
        failures: list[dict[str, str]] = []
        for position, configured_name in enumerate(names):
            if configured_name == "valid_cache":
                continue
            resolved_name = self._resolve_name(dataset, configured_name)
            provider = self.providers.get(resolved_name)
            if provider is None:
                failures.append({"provider": configured_name, "error": "provider not configured"})
                continue
            cache_key = self.cache.key(dataset, provider.name, provider.version, query)
            hit = self.cache.get(cache_key)
            if hit is not None:
                payload = json.loads(hit.path.read_text(encoding="utf-8"))
                artifact = DataArtifact.model_validate(payload)
                artifact.cache_hit = True
                # A materialized raster/vector path is part of the normalized
                # artifact.  Do not replace it with the cache-envelope JSON.
                # Metadata-only artifacts still expose that envelope locally.
                if not artifact.local_path:
                    artifact.local_path = str(hit.path)
                return ToolResult.ok(
                    message="Valid cache hit", artifact=artifact, provenance=[artifact],
                    metrics={"cache_hit": True, "cache_miss": False, "bytes_downloaded": 0, "download_latency_ms": 0},
                )
            started = time.perf_counter()
            try:
                result = provider.fetch(query)
            # Provider adapters may raise transport-, parsing-, or driver-specific
            # exceptions. Normalize all of them here so the next configured
            # provider can take over with provenance preserved.
            except Exception as exc:  # noqa: BLE001
                failures.append({"provider": provider.name, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if result.status == "failed" or result.artifact is None:
                failures.append({"provider": provider.name, "error": result.message or str(result.error_code)})
                continue
            artifact = DataArtifact.model_validate(result.artifact)
            if position > 0:
                result.status = "degraded"
                result.degraded = True
                result.original_provider = self._resolve_name(dataset, names[0])
                result.fallback_provider = provider.name
                result.fallback_reason = failures[-1]["error"] if failures else "primary unavailable"
                artifact.source_degraded = True
                artifact.quality_flag = "degraded"
            data = artifact.model_dump_json(indent=2).encode("utf-8")
            entry = self.cache.put(
                key=cache_key, dataset=dataset, provider=provider.name,
                version=provider.version, data=data, suffix=".json",
                metadata={"query": query},
            )
            if not artifact.local_path:
                artifact.local_path = str(entry.path)
            artifact.checksum_sha256 = entry.checksum
            result.artifact = artifact
            result.provenance = [artifact]
            result.metrics.setdefault("cache_hit", False)
            result.metrics["cache_miss"] = True
            result.metrics.setdefault("download_latency_ms", (time.perf_counter() - started) * 1000)
            return result
        return ToolResult.fail(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"No provider succeeded for {dataset}",
            retryable=True,
            metadata={"provider_failures": failures},
        )

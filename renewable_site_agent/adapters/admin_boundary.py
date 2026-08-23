"""Administrative-boundary provider backed by Nominatim/OSM."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BoundaryResolutionError(RuntimeError):
    """Raised when a valid polygon boundary cannot be resolved."""


@dataclass(frozen=True)
class BoundaryResult:
    geometry: dict
    display_name: str
    source_identifier: str
    source_license: str | None
    bounding_box: list[str]
    metadata: dict


class NominatimBoundaryProvider:
    """Resolve a named region to a real OSM polygon or multipolygon."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        base_url: str | None = None,
        user_agent: str | None = None,
        country_codes: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.base_url = (
            base_url
            or os.getenv(
                "NOMINATIM_BASE_URL",
                "https://nominatim.openstreetmap.org",
            )
        ).rstrip("/")

        self.user_agent = (
            user_agent
            or os.getenv(
                "NOMINATIM_USER_AGENT",
                "RenewableSiteAgent/0.1",
            )
        )

        self.country_codes = (
            country_codes
            if country_codes is not None
            else os.getenv("BOUNDARY_COUNTRY_CODES", "cn")
        )

        self.timeout_seconds = timeout_seconds

    def _cache_path(self, region: str) -> Path:
        cache_key = hashlib.sha256(
            (
                f"{self.base_url}|"
                f"{self.country_codes}|"
                f"{region.strip().lower()}"
            ).encode("utf-8")
        ).hexdigest()

        return self.cache_dir / f"{cache_key}.json"

    def _download(self, region: str) -> list[dict]:
        parameters = {
            "q": region,
            "format": "jsonv2",
            "polygon_geojson": 1,
            "addressdetails": 1,
            "limit": 10,
        }

        if self.country_codes:
            parameters["countrycodes"] = self.country_codes

        request = Request(
            f"{self.base_url}/search?{urlencode(parameters)}",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh,en",
            },
        )

        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            raise BoundaryResolutionError(
                f"Boundary service returned HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise BoundaryResolutionError(
                f"Boundary service connection failed: {exc.reason}"
            ) from exc

        result = json.loads(payload)

        if not isinstance(result, list):
            raise BoundaryResolutionError(
                "Boundary service returned an invalid response."
            )

        return result

    def resolve(self, region: str) -> BoundaryResult:
        cache_path = self._cache_path(region)

        if cache_path.exists():
            results = json.loads(
                cache_path.read_text(encoding="utf-8")
            )
        else:
            results = self._download(region)

            temporary_path = cache_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(
                    results,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary_path.replace(cache_path)

        selected = next(
            (
                item
                for item in results
                if isinstance(item.get("geojson"), dict)
                and item["geojson"].get("type")
                in {"Polygon", "MultiPolygon"}
            ),
            None,
        )

        if selected is None:
            raise BoundaryResolutionError(
                f"No polygon administrative boundary found for: {region}"
            )

        return BoundaryResult(
            geometry=selected["geojson"],
            display_name=selected.get("display_name", region),
            source_identifier=(
                f"osm:{selected.get('osm_type', 'unknown')}/"
                f"{selected.get('osm_id', 'unknown')}"
            ),
            source_license=selected.get("licence"),
            bounding_box=selected.get("boundingbox", []),
            metadata={
                "category": selected.get("category"),
                "type": selected.get("type"),
                "addresstype": selected.get("addresstype"),
                "importance": selected.get("importance"),
                "cache_path": str(cache_path),
            },
        )

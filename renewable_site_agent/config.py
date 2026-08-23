"""Canonical project configuration and D-drive path safety."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = PROJECT_ROOT / "config"


def _require_d_drive() -> bool:
    """Retain the original D-drive policy as an explicit deployment option."""
    return os.getenv("ATLAS_REQUIRE_D_DRIVE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing configuration file: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise TypeError(f"Configuration must be a mapping: {path}")
    return value


def _expand(value: str) -> Path:
    return Path(value.replace("${PROJECT_ROOT}", str(PROJECT_ROOT))).resolve()


@dataclass(frozen=True)
class ProjectPaths:
    project_root: Path
    data: Path
    cache: Path
    runtime: Path
    artifacts: Path
    logs: Path
    state: Path
    temp: Path
    models: Path
    downloads: Path
    database: Path
    max_cache_size_gb: float
    min_free_disk_gb: float

    @classmethod
    def load(cls) -> ProjectPaths:
        raw = _read_yaml("paths.yaml")
        return cls(
            project_root=_expand(raw["project_root"]),
            data=_expand(raw["data_dir"]),
            cache=_expand(raw["cache_dir"]),
            runtime=_expand(raw["runtime_dir"]),
            artifacts=_expand(raw["artifacts_dir"]),
            logs=_expand(raw["logs_dir"]),
            state=_expand(raw["state_dir"]),
            temp=_expand(raw["temp_dir"]),
            models=_expand(raw["models_dir"]),
            downloads=_expand(raw["downloads_dir"]),
            database=_expand(raw["database_path"]),
            max_cache_size_gb=float(raw.get("max_cache_size_gb", 15)),
            min_free_disk_gb=float(raw.get("min_free_disk_gb", 10)),
        )

    def managed_directories(self) -> tuple[Path, ...]:
        return (
            self.data,
            self.cache,
            self.runtime,
            self.artifacts,
            self.logs,
            self.state,
            self.temp,
            self.models,
            self.downloads,
        )

    def ensure(self) -> None:
        require_d_drive = os.name == "nt" and _require_d_drive()
        if require_d_drive and self.project_root.drive.upper() != "D:":
            raise RuntimeError(
                f"Canonical runtime must be on D:, got {self.project_root}"
            )
        for path in self.managed_directories():
            if require_d_drive and path.drive.upper() != "D:":
                raise RuntimeError(f"Managed path is not on D: {path}")
            if os.name != "nt" and not (
                path == self.project_root or self.project_root in path.parents
            ):
                raise RuntimeError(f"Managed path is outside project root: {path}")
            path.mkdir(parents=True, exist_ok=True)

    def assert_artifact_path(self, path: Path, *, large: bool = True) -> Path:
        resolved = path.resolve()
        if (
            large
            and os.name == "nt"
            and _require_d_drive()
            and resolved.drive.upper() != "D:"
        ):
            raise RuntimeError(f"Large artifact outside D: is forbidden: {resolved}")
        allowed = any(
            resolved == root or root in resolved.parents
            for root in self.managed_directories()
        )
        if not allowed:
            raise RuntimeError(f"Artifact path is outside managed roots: {resolved}")
        return resolved

    def free_disk_gb(self) -> float:
        return shutil.disk_usage(self.project_root.anchor).free / (1024**3)

    def guard_disk_space(self, estimated_bytes: int = 0) -> None:
        remaining = self.free_disk_gb() - estimated_bytes / (1024**3)
        if remaining < self.min_free_disk_gb:
            raise RuntimeError(
                "DISK_BUDGET_EXCEEDED: operation would reduce D: below "
                f"{self.min_free_disk_gb:.1f} GB free"
            )

    def process_environment(self) -> dict[str, str]:
        """Return project-scoped environment variables without global mutation."""
        return {
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            "HF_HOME": str(self.cache / "huggingface"),
            "TRANSFORMERS_CACHE": str(self.cache / "huggingface"),
            "TORCH_HOME": str(self.cache / "torch"),
            "PIP_CACHE_DIR": str(self.cache / "pip"),
            "RENEWABLE_SITE_RUNTIME_DIR": str(self.runtime),
        }


PATHS = ProjectPaths.load()


def load_config(name: str) -> dict[str, Any]:
    return _read_yaml(name)


def apply_process_environment() -> None:
    PATHS.ensure()
    for key, value in PATHS.process_environment().items():
        os.environ[key] = value

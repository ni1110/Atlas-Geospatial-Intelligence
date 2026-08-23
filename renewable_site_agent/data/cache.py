"""Content-addressed, checksum-validated cache with a hard LRU size limit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from renewable_site_agent.config import PATHS


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    key: str
    path: Path
    checksum: str
    size_bytes: int
    metadata: dict[str, Any]


class DataCache:
    def __init__(self, root: Path | None = None, max_size_gb: float | None = None) -> None:
        self.root = (root or PATHS.cache / "data").resolve()
        PATHS.assert_artifact_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "cache_index.sqlite3"
        self.max_size_bytes = int((max_size_gb or PATHS.max_cache_size_gb) * 1024**3)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    dataset TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def key(
        dataset: str,
        provider: str,
        version: str,
        query: dict[str, Any],
    ) -> str:
        material = stable_json(
            {"dataset": dataset, "provider": provider, "version": version, "query": query}
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, key: str) -> CacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            path = Path(row["path"])
            if (
                not path.is_file()
                or path.stat().st_size != row["size_bytes"]
                or sha256_file(path) != row["checksum"]
            ):
                connection.execute("DELETE FROM cache_entries WHERE cache_key = ?", (key,))
                return None
            connection.execute(
                "UPDATE cache_entries SET accessed_at = ? WHERE cache_key = ?",
                (datetime.now(UTC).isoformat(), key),
            )
        return CacheEntry(
            key=key,
            path=path,
            checksum=row["checksum"],
            size_bytes=row["size_bytes"],
            metadata=json.loads(row["metadata_json"]),
        )

    def put(
        self,
        *,
        key: str,
        dataset: str,
        provider: str,
        version: str,
        data: bytes,
        suffix: str,
        metadata: dict[str, Any],
    ) -> CacheEntry:
        PATHS.guard_disk_space(len(data))
        dataset_dir = self.root / dataset
        dataset_dir.mkdir(parents=True, exist_ok=True)
        destination = PATHS.assert_artifact_path(dataset_dir / f"{key}{suffix}")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(data)
        checksum = sha256_bytes(data)
        temporary.replace(destination)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries(
                    cache_key,dataset,provider,version,path,checksum,size_bytes,
                    created_at,accessed_at,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    path=excluded.path,checksum=excluded.checksum,
                    size_bytes=excluded.size_bytes,accessed_at=excluded.accessed_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    key, dataset, provider, version, str(destination), checksum,
                    len(data), now, now, stable_json(metadata),
                ),
            )
        self.enforce_limit()
        return CacheEntry(key, destination, checksum, len(data), metadata)

    def enforce_limit(self) -> int:
        removed = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cache_entries ORDER BY accessed_at ASC"
            ).fetchall()
            total = sum(int(row["size_bytes"]) for row in rows)
            for row in rows:
                if total <= self.max_size_bytes:
                    break
                path = Path(row["path"]).resolve()
                if self.root not in path.parents:
                    continue
                if path.is_file():
                    path.unlink()
                connection.execute(
                    "DELETE FROM cache_entries WHERE cache_key = ?", (row["cache_key"],)
                )
                total -= int(row["size_bytes"])
                removed += 1
        return removed

    def stats(self) -> dict[str, int | float]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS bytes FROM cache_entries"
            ).fetchone()
        return {
            "entry_count": int(row["count"]),
            "size_bytes": int(row["bytes"]),
            "max_size_bytes": self.max_size_bytes,
            "utilization": int(row["bytes"]) / self.max_size_bytes if self.max_size_bytes else 0,
        }

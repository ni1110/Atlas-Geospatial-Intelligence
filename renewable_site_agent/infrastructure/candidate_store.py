"""SQLite repository for candidate-site persistence."""

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from renewable_site_agent.domain.candidates import CandidateSite


class SQLiteCandidateStore:
    """Persist and query candidate sites using the project SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_sites (
                    candidate_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    screening_status TEXT NOT NULL,
                    area_km2 REAL NOT NULL,
                    centroid_lon REAL NOT NULL,
                    centroid_lat REAL NOT NULL,
                    geometry_sha256 TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY(job_id)
                        REFERENCES jobs(job_id)
                        ON DELETE CASCADE,

                    UNIQUE(job_id, geometry_sha256)
                );

                CREATE INDEX IF NOT EXISTS
                    idx_candidate_sites_job_rank
                ON candidate_sites(job_id, rank);

                CREATE INDEX IF NOT EXISTS
                    idx_candidate_sites_job_status
                ON candidate_sites(job_id, screening_status);
                """
            )

    def replace_for_job(
        self,
        job_id: str,
        candidates: Iterable[CandidateSite],
    ) -> int:
        """
        Atomically replace the current candidate set for one job.

        Re-running the same generation is idempotent:
        existing rows are updated and stale rows are removed.
        """
        candidate_list = list(candidates)

        for candidate in candidate_list:
            if candidate.job_id != job_id:
                raise ValueError(
                    "Candidate job_id does not match repository job_id: "
                    f"{candidate.candidate_id}"
                )

        candidate_ids = [
            candidate.candidate_id
            for candidate in candidate_list
        ]

        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError(
                "Candidate set contains duplicate candidate_id values."
            )

        with closing(self._connect()) as connection, connection:
            job_exists = connection.execute(
                """
                SELECT 1
                FROM jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()

            if job_exists is None:
                raise KeyError(
                    f"Cannot save candidates for unknown job: {job_id}"
                )

            for candidate in candidate_list:
                payload_json = json.dumps(
                    candidate.model_dump(mode="json"),
                    ensure_ascii=False,
                )

                connection.execute(
                    """
                    INSERT INTO candidate_sites (
                        candidate_id,
                        job_id,
                        rank,
                        stage,
                        screening_status,
                        area_km2,
                        centroid_lon,
                        centroid_lat,
                        geometry_sha256,
                        artifact_path,
                        source_evidence_id,
                        payload_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)

                    ON CONFLICT(candidate_id) DO UPDATE SET
                        rank = excluded.rank,
                        stage = excluded.stage,
                        screening_status = excluded.screening_status,
                        area_km2 = excluded.area_km2,
                        centroid_lon = excluded.centroid_lon,
                        centroid_lat = excluded.centroid_lat,
                        geometry_sha256 = excluded.geometry_sha256,
                        artifact_path = excluded.artifact_path,
                        source_evidence_id = excluded.source_evidence_id,
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        candidate.candidate_id,
                        candidate.job_id,
                        candidate.rank,
                        candidate.stage.value,
                        candidate.screening_status.value,
                        candidate.area_km2,
                        candidate.centroid_lon,
                        candidate.centroid_lat,
                        candidate.geometry_sha256,
                        candidate.artifact_path,
                        candidate.source_evidence_id,
                        payload_json,
                        candidate.created_at.isoformat(),
                    ),
                )

            if candidate_ids:
                placeholders = ",".join(
                    "?" for _ in candidate_ids
                )

                connection.execute(
                    f"""
                    DELETE FROM candidate_sites
                    WHERE job_id = ?
                      AND candidate_id NOT IN ({placeholders})
                    """,
                    (job_id, *candidate_ids),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM candidate_sites
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )

        return len(candidate_list)

    def get(self, candidate_id: str) -> CandidateSite:
        """Return one candidate by its stable identifier."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM candidate_sites
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()

        if row is None:
            raise KeyError(
                f"Candidate not found: {candidate_id}"
            )

        return CandidateSite.model_validate_json(
            row["payload_json"]
        )

    def list_by_job(
        self,
        job_id: str,
    ) -> list[CandidateSite]:
        """Return candidates ordered by their business rank."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM candidate_sites
                WHERE job_id = ?
                ORDER BY rank ASC, candidate_id ASC
                """,
                (job_id,),
            ).fetchall()

        return [
            CandidateSite.model_validate_json(
                row["payload_json"]
            )
            for row in rows
        ]

    def count_by_job(self, job_id: str) -> int:
        """Return the number of current candidates for one job."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS candidate_count
                FROM candidate_sites
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()

        return int(row["candidate_count"])

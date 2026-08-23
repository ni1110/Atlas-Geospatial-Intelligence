"""SQLite persistence for jobs and execution events."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from renewable_site_agent.domain.jobs import SiteSelectionJob


class SQLiteJobStore:
    """Persist the latest job state and append-only job events."""

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
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE INDEX IF NOT EXISTS idx_job_events_job_id
                ON job_events(job_id);
                """
            )

    def save(
        self,
        job: SiteSelectionJob,
        *,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        job_json = json.dumps(
            job.model_dump(mode="json"),
            ensure_ascii=False,
        )

        event_json = json.dumps(
            event_payload or {},
            ensure_ascii=False,
        )

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    status,
                    payload_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.job_id,
                    job.status.value,
                    job_json,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )

            connection.execute(
                """
                INSERT INTO job_events (
                    job_id,
                    event_type,
                    event_time,
                    payload_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    event_type,
                    job.updated_at.isoformat(),
                    event_json,
                ),
            )

    def get(self, job_id: str) -> SiteSelectionJob:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()

        if row is None:
            raise KeyError(f"Job not found: {job_id}")

        return SiteSelectionJob.model_validate_json(
            row["payload_json"]
        )

    def list_events(
        self,
        job_id: str,
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT
                    event_id,
                    event_type,
                    event_time,
                    payload_json
                FROM job_events
                WHERE job_id = ?
                ORDER BY event_id
                """,
                (job_id,),
            ).fetchall()

        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "event_time": row["event_time"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

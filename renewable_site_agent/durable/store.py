"""Durable SQLite queue, leases, checkpoints, idempotency, and provider health."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from renewable_site_agent.config import PATHS
from renewable_site_agent.data.cache import sha256_file, stable_json


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    PENDING = "PENDING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    PAUSED = "PAUSED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    LOOP_DETECTED = "LOOP_DETECTED"


TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.BUDGET_EXCEEDED,
    JobStatus.LOOP_DETECTED, JobStatus.NEEDS_HUMAN_REVIEW,
}


class DurableJobStore:
    def __init__(self, database_path: Path | None = None) -> None:
        PATHS.ensure()
        self.database_path = (database_path or PATHS.database).resolve()
        PATHS.assert_artifact_path(self.database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    request_text TEXT NOT NULL,
                    request_json TEXT,
                    route_json TEXT NOT NULL,
                    plan_json TEXT,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    total_steps INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error_json TEXT,
                    worker_id TEXT,
                    locked_until TEXT,
                    heartbeat_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON jobs(status, locked_until, created_at);

                CREATE TABLE IF NOT EXISTS job_steps (
                    job_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    artifact_path TEXT,
                    artifact_hash TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error_json TEXT,
                    PRIMARY KEY(job_id, step_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS tool_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    provider TEXT,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    latency_ms REAL,
                    retry INTEGER NOT NULL DEFAULT 0,
                    fallback INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    artifact_path TEXT,
                    artifact_hash TEXT,
                    state_snapshot_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    UNIQUE(job_id, step_id, input_hash),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    step_id TEXT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, kind, path),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, event_id);

                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def request_key(request_text: str, client_key: str | None = None) -> str:
        if client_key:
            return hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        normalized = " ".join(request_text.strip().lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def create_job(
        self,
        *,
        request_text: str,
        route: dict[str, Any],
        request: dict[str, Any] | None,
        plan: dict[str, Any] | None,
        status: JobStatus,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        key = self.request_key(request_text, idempotency_key)
        job_id = str(uuid4())
        now = utc_now().isoformat()
        total_steps = len((plan or {}).get("steps", []))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                return self._job_dict(existing), True
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id,idempotency_key,request_text,request_json,route_json,
                    plan_json,status,total_steps,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, key, request_text,
                    stable_json(request) if request is not None else None,
                    stable_json(route), stable_json(plan) if plan is not None else None,
                    status.value, total_steps, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (job_id, "job_created", stable_json({"status": status.value}), now),
            )
            connection.execute("COMMIT")
        return self.get_job(job_id), False

    def claim(self, worker_id: str, lease_seconds: int = 90) -> dict[str, Any] | None:
        now = utc_now()
        locked_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('PENDING','RUNNING','RETRYING')
                  AND (locked_until IS NULL OR locked_until < ?)
                ORDER BY CASE status WHEN 'RUNNING' THEN 0 WHEN 'RETRYING' THEN 1 ELSE 2 END,
                         created_at
                LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                UPDATE jobs SET status='RUNNING',worker_id=?,locked_until=?,
                    heartbeat_at=?,updated_at=? WHERE job_id=?
                """,
                (worker_id, locked_until, now.isoformat(), now.isoformat(), row["job_id"]),
            )
            event = "worker_resumed" if row["status"] in {"RUNNING", "RETRYING"} else "worker_claimed"
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (row["job_id"], event, stable_json({"worker_id": worker_id}), now.isoformat()),
            )
            connection.execute("COMMIT")
        return self.get_job(row["job_id"])

    def clarify_job(
        self,
        job_id: str,
        *,
        request_text: str,
        route: dict[str, Any],
        request: dict[str, Any],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if current is None:
                connection.execute("ROLLBACK")
                raise KeyError(job_id)
            if current["status"] not in {"NEEDS_CLARIFICATION", "PAUSED", "NEEDS_HUMAN_REVIEW"}:
                connection.execute("ROLLBACK")
                raise ValueError(f"Job cannot accept clarification in status {current['status']}")
            connection.execute(
                """UPDATE jobs SET request_text=?,request_json=?,route_json=?,plan_json=?,
                   status='PENDING',current_step=NULL,total_steps=?,completed_steps=0,
                   progress=0,result_json=NULL,error_json=NULL,worker_id=NULL,
                   locked_until=NULL,updated_at=? WHERE job_id=?""",
                (
                    request_text, stable_json(request), stable_json(route), stable_json(plan),
                    len(plan.get("steps", [])), now, job_id,
                ),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (job_id, "clarification_received", stable_json({"status": "PENDING"}), now),
            )
            connection.execute("COMMIT")
        return self.get_job(job_id)

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 90) -> bool:
        now = utc_now()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE jobs SET heartbeat_at=?,locked_until=?,updated_at=?
                   WHERE job_id=? AND worker_id=? AND status='RUNNING'""",
                (
                    now.isoformat(), (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(), job_id, worker_id,
                ),
            )
        return cursor.rowcount == 1

    def start_step(self, job_id: str, step_id: str, tool_name: str, input_hash: str) -> int:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt FROM job_steps WHERE job_id=? AND step_id=?",
                (job_id, step_id),
            ).fetchone()
            attempt = int(row["attempt"] if row else 0) + 1
            connection.execute(
                """
                INSERT INTO job_steps(job_id,step_id,tool_name,input_hash,status,attempt,started_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id,step_id) DO UPDATE SET
                    tool_name=excluded.tool_name,input_hash=excluded.input_hash,
                    status='RUNNING',attempt=excluded.attempt,started_at=excluded.started_at,
                    error_json=NULL
                """,
                (job_id, step_id, tool_name, input_hash, "RUNNING", attempt, now),
            )
            connection.execute(
                "UPDATE jobs SET current_step=?,updated_at=? WHERE job_id=?",
                (step_id, now, job_id),
            )
            connection.execute("COMMIT")
        return attempt

    def checkpoint(
        self,
        *,
        job_id: str,
        step_id: str,
        tool_name: str,
        input_hash: str,
        attempt: int,
        state_snapshot: dict[str, Any],
        artifact_path: str | None = None,
        artifact_hash: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO checkpoints(
                    job_id,step_id,tool_name,input_hash,status,attempt,artifact_path,
                    artifact_hash,state_snapshot_json,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,step_id,input_hash) DO UPDATE SET
                    status='SUCCESS',attempt=excluded.attempt,
                    artifact_path=excluded.artifact_path,artifact_hash=excluded.artifact_hash,
                    state_snapshot_json=excluded.state_snapshot_json,
                    completed_at=excluded.completed_at
                """,
                (
                    job_id, step_id, tool_name, input_hash, "SUCCESS", attempt,
                    artifact_path, artifact_hash, stable_json(state_snapshot), now,
                ),
            )
            connection.execute(
                """UPDATE job_steps SET status='SUCCESS',artifact_path=?,artifact_hash=?,
                   completed_at=? WHERE job_id=? AND step_id=?""",
                (artifact_path, artifact_hash, now, job_id, step_id),
            )
            completed = connection.execute(
                "SELECT COUNT(*) AS n FROM job_steps WHERE job_id=? AND status='SUCCESS'",
                (job_id,),
            ).fetchone()["n"]
            total = connection.execute(
                "SELECT total_steps FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()["total_steps"]
            progress = float(completed) / float(total) if total else 0
            connection.execute(
                """UPDATE jobs SET completed_steps=?,progress=?,updated_at=?
                   WHERE job_id=?""",
                (completed, progress, now, job_id),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (job_id, "checkpoint_written", stable_json({"step_id": step_id, "input_hash": input_hash}), now),
            )
            connection.execute("COMMIT")

    def reusable_checkpoint(self, job_id: str, step_id: str, input_hash: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT * FROM checkpoints WHERE job_id=? AND step_id=?
                   AND input_hash=? AND status='SUCCESS' ORDER BY checkpoint_id DESC LIMIT 1""",
                (job_id, step_id, input_hash),
            ).fetchone()
        if row is None:
            return None
        artifact_path = row["artifact_path"]
        artifact_hash = row["artifact_hash"]
        if artifact_path:
            path = Path(artifact_path)
            if not path.is_file() or (artifact_hash and sha256_file(path) != artifact_hash):
                return None
        result = dict(row)
        result["state_snapshot"] = json.loads(result.pop("state_snapshot_json"))
        return result

    def record_attempt(self, job_id: str, step_id: str, value: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tool_attempts(
                    job_id,step_id,tool_name,provider,attempt,status,error_code,
                    latency_ms,retry,fallback,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, step_id, value.get("tool_name"), value.get("provider"),
                    int(value.get("attempt", 1)), value.get("status", "unknown"),
                    value.get("error_code"), value.get("latency_ms"),
                    int(value.get("attempt", 1) > 1), int(bool(value.get("fallback"))), now,
                ),
            )

    def add_artifact(
        self,
        *,
        job_id: str,
        step_id: str | None,
        kind: str,
        path: Path,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        resolved = PATHS.assert_artifact_path(path)
        checksum = sha256_file(resolved)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO artifacts(job_id,step_id,kind,path,checksum,size_bytes,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,kind,path) DO UPDATE SET
                    checksum=excluded.checksum,size_bytes=excluded.size_bytes,
                    metadata_json=excluded.metadata_json
                """,
                (
                    job_id, step_id, kind, str(resolved), checksum, resolved.stat().st_size,
                    stable_json(metadata or {}), utc_now().isoformat(),
                ),
            )
        return checksum

    def finish(self, job_id: str, status: JobStatus, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        if status not in TERMINAL_STATUSES and status not in {JobStatus.PAUSED, JobStatus.NEEDS_CLARIFICATION}:
            raise ValueError(f"Invalid finish status: {status}")
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE jobs SET status=?,result_json=?,error_json=?,progress=?,
                   worker_id=NULL,locked_until=NULL,updated_at=? WHERE job_id=?""",
                (
                    status.value, stable_json(result) if result is not None else None,
                    stable_json(error) if error is not None else None,
                    1.0 if status == JobStatus.SUCCEEDED else 0.0, now, job_id,
                ),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (job_id, "job_finished", stable_json({"status": status.value, "error": error}), now),
            )

    def append_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (job_id, event_type, stable_json(payload), utc_now().isoformat()),
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_dict(row)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY event_id", (job_id,)
            ).fetchall()
        return [
            {
                "event_id": row["event_id"], "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]), "created_at": row["created_at"],
            }
            for row in rows
        ]

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM job_steps WHERE job_id=? ORDER BY started_at,step_id", (job_id,)
            ).fetchall()]

    def artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id=? ORDER BY artifact_id", (job_id,)
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metadata"] = json.loads(value.pop("metadata_json"))
            values.append(value)
        return values

    def get_provider_health(self, provider: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT state_json FROM provider_health WHERE provider=?", (provider,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def set_provider_health(self, provider: str, value: dict[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO provider_health(provider,state_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET
                   state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (provider, stable_json(value), utc_now().isoformat()),
            )

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("request_json", "route_json", "plan_json", "result_json", "error_json"):
            if value.get(key):
                value[key.removesuffix("_json")] = json.loads(value[key])
            else:
                value[key.removesuffix("_json")] = None
            value.pop(key, None)
        return value

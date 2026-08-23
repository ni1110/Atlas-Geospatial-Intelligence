"""Durable multi-agent tables in the existing GeoAgent SQLite database."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from geoagent.orchestration.models import AgentResult, GoalContract, OrchestrationResult, ReviewResult, TaskGraph, utc_now
from renewable_site_agent.config import PATHS
from renewable_site_agent.durable.store import DurableJobStore


class MultiAgentStateStore:
    """Extends, rather than replaces, the canonical durable SQLite store."""

    def __init__(self, database: Path | None = None) -> None:
        self.database = (database or PATHS.database).resolve()
        PATHS.assert_artifact_path(self.database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        # Reuse the canonical durable schema first (jobs, tool_attempts,
        # checkpoints, provider_health, events) and extend that same database
        # with the multi-agent tables below.
        DurableJobStore(self.database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                step_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL,
                checksum TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(job_id, kind, path))""",
            """CREATE TABLE IF NOT EXISTS goal_contract_versions (
                goal_id TEXT NOT NULL, version INTEGER NOT NULL, job_id TEXT NOT NULL,
                contract_hash TEXT NOT NULL, contract_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(goal_id, version))""",
            """CREATE TABLE IF NOT EXISTS task_graphs (
                graph_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, job_id TEXT NOT NULL,
                plan_hash TEXT NOT NULL, graph_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS task_nodes (
                graph_id TEXT NOT NULL, task_id TEXT NOT NULL, task_signature TEXT NOT NULL,
                agent_role TEXT NOT NULL, status TEXT NOT NULL, node_json TEXT NOT NULL,
                artifact_ids_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
                PRIMARY KEY(graph_id, task_id))""",
            """CREATE TABLE IF NOT EXISTS agent_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                graph_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT,
                latency_ms REAL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, task_id TEXT NOT NULL,
                source TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                graph_id TEXT NOT NULL, review_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS replans (
                replan_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, graph_id TEXT NOT NULL,
                patch_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workspace_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, action TEXT NOT NULL, path TEXT,
                decision TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS policy_denials (
                denial_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, error_code TEXT NOT NULL, attempted_action TEXT NOT NULL,
                path TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL)""",
        ]
        with closing(self._connect()) as connection, connection:
            for statement in statements:
                connection.execute(statement)

    @staticmethod
    def _json(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))

    def save_contract(self, job_id: str, contract: GoalContract) -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO jobs(
                    job_id,idempotency_key,request_text,request_json,route_json,status,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id, f"multi-agent:{job_id}", contract.primary_goal,
                    self._json(contract), self._json({"intent": contract.intent, "subtype": contract.subtype}),
                    "PLANNED", now, now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO goal_contract_versions VALUES(?,?,?,?,?,?)",
                (contract.goal_id, contract.version, job_id, contract.contract_hash, self._json(contract), now),
            )

    def save_graph(self, job_id: str, graph: TaskGraph, status: str = "PLANNED") -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO task_graphs VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(graph_id) DO UPDATE SET graph_json=excluded.graph_json,
                   status=excluded.status,updated_at=excluded.updated_at""",
                (graph.graph_id, graph.goal_id, job_id, graph.plan_hash, self._json(graph), status, now, now),
            )
            for node in graph.nodes:
                connection.execute(
                    """INSERT INTO task_nodes(graph_id,task_id,task_signature,agent_role,status,node_json,artifact_ids_json,updated_at)
                       VALUES(?,?,?,?,?,?,'[]',?) ON CONFLICT(graph_id,task_id) DO UPDATE SET
                       status=excluded.status,node_json=excluded.node_json,updated_at=excluded.updated_at""",
                    (graph.graph_id, node.task_id, node.task_signature, node.agent_role, node.status.value, self._json(node), now),
                )
            connection.execute(
                "UPDATE jobs SET plan_json=?,total_steps=?,updated_at=? WHERE job_id=?",
                (self._json(graph), len(graph.nodes), now, job_id),
            )

    def save_attempt(self, job_id: str, graph_id: str, result: AgentResult, attempt: int, latency_ms: float) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO agent_attempts(job_id,graph_id,task_id,agent_id,attempt,status,result_json,latency_ms,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, graph_id, result.task_id, result.agent_id, attempt, result.status, self._json(result), latency_ms, utc_now().isoformat()),
            )
            for evidence in result.evidence_refs:
                connection.execute(
                    "INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?)",
                    (evidence.evidence_id, job_id, result.task_id, evidence.source, self._json(evidence), utc_now().isoformat()),
                )

    def save_artifact(self, artifact: Any) -> None:
        path = Path(artifact.path)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO artifacts(job_id,step_id,kind,path,checksum,size_bytes,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,kind,path) DO UPDATE SET
                   checksum=excluded.checksum,size_bytes=excluded.size_bytes,metadata_json=excluded.metadata_json""",
                (
                    artifact.job_id, artifact.task_id, artifact.artifact_type, artifact.path,
                    artifact.checksum_sha256, path.stat().st_size,
                    self._json({"artifact_id": artifact.artifact_id, "producer_agent": artifact.producer_agent, "provenance": artifact.provenance}),
                    utc_now().isoformat(),
                ),
            )

    def save_replan(self, job_id: str, patch: Any) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO replans VALUES(?,?,?,?,?,?)",
                (patch.patch_id, job_id, patch.graph_id, self._json(patch), patch.reason, utc_now().isoformat()),
            )

    def save_result(self, result: OrchestrationResult) -> None:
        """Persist the complete formal-entry result in the canonical jobs row."""
        now = utc_now().isoformat()
        status = result.status
        progress = 1.0 if status == "SUCCEEDED" else 0.0
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET status=?,result_json=?,progress=?,completed_steps=?,updated_at=? WHERE job_id=?",
                (status, self._json(result), progress, len(result.agent_results), now, result.job_id),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (result.job_id, "geo_orchestration_finished", self._json({"status": status}), now),
            )

    def execution_snapshot(self, job_id: str) -> dict[str, Any]:
        """Return planned and actually observed execution as separate fields."""
        durable = DurableJobStore(self.database)
        job = durable.get_job(job_id)
        with closing(self._connect()) as connection:
            contract_row = connection.execute(
                "SELECT contract_json FROM goal_contract_versions WHERE job_id=? ORDER BY version DESC LIMIT 1", (job_id,),
            ).fetchone()
            graph_rows = connection.execute(
                "SELECT graph_json,status FROM task_graphs WHERE job_id=? ORDER BY created_at", (job_id,),
            ).fetchall()
            attempt_rows = connection.execute(
                "SELECT attempt_id,graph_id,task_id,agent_id,attempt,status,result_json,latency_ms,created_at FROM agent_attempts WHERE job_id=? ORDER BY attempt_id", (job_id,),
            ).fetchall()
            review_rows = connection.execute(
                "SELECT review_json FROM reviews WHERE job_id=? ORDER BY review_id", (job_id,),
            ).fetchall()
            replan_rows = connection.execute(
                "SELECT patch_json FROM replans WHERE job_id=? ORDER BY created_at", (job_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT evidence_json FROM evidence WHERE job_id=? ORDER BY created_at", (job_id,),
            ).fetchall()
        result = job.get("result") or {}
        durable_artifacts = durable.artifacts(job_id)
        graph = result.get("task_graph") or (json.loads(graph_rows[-1]["graph_json"]) if graph_rows else job.get("plan"))
        attempts: list[dict[str, Any]] = []
        actual_calls: list[dict[str, Any]] = []
        for row in attempt_rows:
            item = dict(row)
            parsed_result = json.loads(item.pop("result_json")) if item.get("result_json") else None
            item["result"] = parsed_result
            attempts.append(item)
            for call in (parsed_result or {}).get("actual_tool_calls", []):
                normalized_call = dict(call)
                normalized_call["execution_class"] = {
                    "LIVE": "OFFLINE_LOCAL_DATA_EXECUTED",
                    "CACHE": "CACHE_REUSED",
                    "OFFLINE": "OFFLINE_LOCAL_DATA_EXECUTED",
                }.get(str(normalized_call.get("execution_class")), normalized_call.get("execution_class"))
                actual_calls.append({"task_id": item["task_id"], "agent_id": item["agent_id"], **normalized_call})
        planned_nodes = (graph or {}).get("nodes", [])
        return {
            "job_id": job_id,
            "status": job["status"],
            "route_mode": (result.get("metrics") or {}).get("route_mode"),
            "plan_mode": (graph or {}).get("planner_mode") or (result.get("metrics") or {}).get("plan_mode"),
            "intent": result.get("intent") or job.get("route"),
            "goal_contract": json.loads(contract_row["contract_json"]) if contract_row else None,
            "task_graph": graph,
            "selected_agents": sorted({node.get("agent_role") for node in planned_nodes if node.get("agent_role")}),
            "planned_tools": [{"task_id": node.get("task_id"), "allowed_tools": node.get("allowed_tools", [])} for node in planned_nodes],
            "actual_attempts": attempts,
            "actual_tool_calls": actual_calls,
            "execution_classes": sorted({call.get("execution_class", "UNKNOWN") for call in actual_calls}),
            "artifacts": durable_artifacts,
            # Additive, presentation-only previews. Existing API fields and
            # artifact semantics remain unchanged; only checksum-valid JSON
            # below this job's governed runtime directory is inspected.
            "artifact_previews": self._artifact_previews(job_id, durable_artifacts),
            "evidence": [json.loads(row["evidence_json"]) for row in evidence_rows],
            "review": result.get("review"),
            "reviews": [json.loads(row["review_json"]) for row in review_rows],
            "replans": [json.loads(row["patch_json"]) for row in replan_rows],
            "final_artifacts": result.get("final_artifacts", []),
            "metrics": result.get("metrics", {}),
        }

    @staticmethod
    def _artifact_previews(job_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Match the single governed WorkspaceManager root. PATHS.runtime is
        # the renewable compatibility runtime (runtime/canonical), whereas
        # formal GeoAgent workspaces intentionally live in runtime/jobs.
        job_root = (PATHS.project_root / "runtime" / "jobs" / job_id).resolve()
        previews: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        total_bytes = 0
        for artifact in artifacts:
            if len(previews) >= 24 or total_bytes >= 4 * 1024 * 1024:
                break
            try:
                path = Path(str(artifact.get("path", ""))).resolve(strict=True)
                if job_root not in path.parents or path.suffix.lower() != ".json":
                    continue
                size = path.stat().st_size
                if size > 2 * 1024 * 1024:
                    continue
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                if checksum != artifact.get("checksum"):
                    continue
                signature = (str(artifact.get("step_id")), checksum)
                if signature in seen:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                outputs = payload.get("actual_outputs", []) if isinstance(payload, dict) else []
                if not isinstance(outputs, list):
                    outputs = [outputs]
                previews.append({
                    "artifact_id": artifact.get("artifact_id"),
                    "step_id": artifact.get("step_id"),
                    "kind": artifact.get("kind"),
                    "outputs": outputs,
                })
                seen.add(signature)
                total_bytes += size
            except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return previews

    def update_node(self, graph_id: str, task_id: str, status: str, artifact_ids: list[str] | None = None) -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE task_nodes SET status=?,artifact_ids_json=?,updated_at=? WHERE graph_id=? AND task_id=?",
                (status, self._json(artifact_ids or []), now, graph_id, task_id),
            )
            if status in {"SUCCEEDED", "REUSED"}:
                row = connection.execute(
                    """SELECT tg.job_id,tn.task_signature,tn.agent_role
                       FROM task_nodes tn JOIN task_graphs tg ON tg.graph_id=tn.graph_id
                       WHERE tn.graph_id=? AND tn.task_id=?""",
                    (graph_id, task_id),
                ).fetchone()
                if row:
                    connection.execute(
                        """INSERT INTO checkpoints(
                            job_id,step_id,tool_name,input_hash,status,attempt,
                            state_snapshot_json,completed_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(job_id,step_id,input_hash) DO UPDATE SET
                            status=excluded.status,state_snapshot_json=excluded.state_snapshot_json,
                            completed_at=excluded.completed_at""",
                        (
                            row["job_id"], task_id, f"agent:{row['agent_role']}", row["task_signature"],
                            "SUCCESS", 1, self._json({"graph_id": graph_id, "status": status, "artifact_ids": artifact_ids or []}), now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (row["job_id"], "multi_agent_checkpoint_written", self._json({"graph_id": graph_id, "task_id": task_id, "status": status}), now),
                    )

    def save_review(self, job_id: str, graph_id: str, review: ReviewResult) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO reviews(job_id,graph_id,review_json,status,created_at) VALUES(?,?,?,?,?)",
                (job_id, graph_id, self._json(review), review.status, utc_now().isoformat()),
            )

    def record_policy(self, decision: Any) -> None:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO workspace_events(job_id,agent_id,action,path,decision,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (decision.job_id, decision.agent_id, decision.attempted_action, decision.path, "ALLOW" if decision.allowed else "DENY", self._json(decision), now),
            )
            if not decision.allowed:
                connection.execute(
                    "INSERT INTO policy_denials(job_id,agent_id,error_code,attempted_action,path,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (decision.job_id, decision.agent_id, decision.error_code, decision.attempted_action, decision.path, decision.reason, now),
                )

    def reusable_result(self, task_signature: str) -> AgentResult | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT aa.result_json FROM task_nodes tn JOIN agent_attempts aa
                   ON tn.graph_id=aa.graph_id AND tn.task_id=aa.task_id
                   WHERE tn.task_signature=? AND aa.status IN ('SUCCEEDED','REUSED')
                   ORDER BY aa.attempt_id DESC LIMIT 1""",
                (task_signature,),
            ).fetchone()
        if not row or not row["result_json"]:
            return None
        result = AgentResult.model_validate_json(row["result_json"])
        if all(Path(item.path).is_file() and sha256_path(Path(item.path)) == item.checksum_sha256 for item in result.artifact_refs):
            return result.model_copy(update={"status": "REUSED"})
        return None

    def restore_graph(self, graph_id: str) -> TaskGraph:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT graph_json FROM task_graphs WHERE graph_id=?", (graph_id,)).fetchone()
            if not row:
                raise KeyError(graph_id)
            status_rows = connection.execute("SELECT task_id,status FROM task_nodes WHERE graph_id=?", (graph_id,)).fetchall()
        graph = TaskGraph.model_validate_json(row["graph_json"])
        statuses = {item["task_id"]: item["status"] for item in status_rows}
        graph.nodes = [node.model_copy(update={"status": statuses.get(node.task_id, node.status)}) for node in graph.nodes]
        return graph

    def table_counts(self) -> dict[str, int]:
        names = ["goal_contract_versions", "task_graphs", "task_nodes", "agent_attempts", "tool_attempts", "artifacts", "evidence", "reviews", "replans", "workspace_events", "policy_denials", "checkpoints", "provider_health", "events"]
        values: dict[str, int] = {}
        with closing(self._connect()) as connection:
            for name in names:
                try:
                    values[name] = int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                except sqlite3.OperationalError:
                    values[name] = 0
        return values


def sha256_path(path: Path) -> str:
    digest = __import__("hashlib").sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

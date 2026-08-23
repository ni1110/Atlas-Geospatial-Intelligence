"""Non-prompt workspace ACL and deterministic artifact commit gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from geoagent.orchestration.models import (
    AgentCapabilityPolicy,
    ArtifactRef,
    GoalContract,
    PolicyDecision,
)
from renewable_site_agent.config import PATHS
from renewable_site_agent.observability import TELEMETRY


class PolicyDenied(PermissionError):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(f"{decision.error_code}: {decision.reason}")
        self.decision = decision


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class JobWorkspace:
    job_id: str
    root: Path
    shared_inputs: Path
    agents: Path
    staging: Path
    final: Path
    task_state: Path

    def scratch(self, agent_id: str) -> Path:
        return self.agents / agent_id / "scratch"

    def agent_staging(self, agent_id: str) -> Path:
        return self.staging / agent_id


def default_capabilities(workspace: JobWorkspace) -> dict[str, AgentCapabilityPolicy]:
    tool_map = {
        "data": ("search_catalog", "resolve_boundary", "acquire_aoi_window", "provider_cache_lookup"),
        "gis_analysis": ("buffer", "intersect", "clip", "dissolve", "spatial_join", "zonal_statistics", "nearest_distance", "spatial_exclusion"),
        "terrain": ("acquire_dem", "calculate_slope", "calculate_aspect", "terrain_statistics", "basic_hydrology"),
        "remote_sensing": ("search_sentinel", "select_bands", "read_raster_window", "calculate_ndvi", "create_rgb_chip", "optional_vlm_verify"),
        "renewable_siting": ("score_candidates", "rank_candidates", "capacity_area_check"),
        "map_report": ("add_result_to_map", "render_map", "generate_report"),
        "reviewer": ("validate_schema", "validate_geometry", "validate_provenance", "check_goal_constraints"),
        "orchestrator": (),
    }
    providers = {
        "data": ("geoboundaries", "copernicus_glo30", "esa_worldcover", "jrc_gsw", "osm_overpass", "nasa_power", "copernicus_dataspace_stac", "planetary_computer_stac"),
        "terrain": ("copernicus_glo30",),
        "remote_sensing": ("copernicus_dataspace_stac", "planetary_computer_stac"),
    }
    artifact_types = {
        "data": ("data_artifact", "boundary", "catalog_result"),
        "gis_analysis": ("vector", "raster", "gis_result"),
        "terrain": ("dem", "slope", "aspect", "terrain_statistics"),
        "remote_sensing": ("stac_items", "raster_chip", "index_raster", "remote_evidence"),
        "renewable_siting": ("candidate_scores", "candidate_result"),
        "map_report": ("map", "report", "final_result"),
        "reviewer": ("review_result",),
        "orchestrator": ("final_result", "report", "map"),
    }
    values: dict[str, AgentCapabilityPolicy] = {}
    for role, tools in tool_map.items():
        agent_id = f"{role}-agent"
        writes = (str(workspace.scratch(agent_id)), str(workspace.agent_staging(agent_id)))
        reads = (str(workspace.shared_inputs), str(workspace.scratch(agent_id)), str(workspace.staging))
        values[role] = AgentCapabilityPolicy(
            agent_role=role,  # type: ignore[arg-type]
            allowed_agents=tuple(item for item in tool_map if item != "orchestrator") if role == "orchestrator" else (),  # type: ignore[arg-type]
            allowed_tools=tools,
            allowed_read_paths=reads,
            allowed_write_paths=writes,
            allowed_providers=providers.get(role, ()),
            allowed_artifact_types=artifact_types[role],
            max_tool_calls=30 if role == "orchestrator" else (0 if role == "reviewer" else 12),
            max_runtime_seconds=900 if role == "orchestrator" else 300,
            can_delegate=role == "orchestrator",
            can_commit_final=False,
        )
    return values


@dataclass
class WorkspaceManager:
    runtime_root: Path = PATHS.project_root / "runtime" / "jobs"
    audit_hook: Callable[[PolicyDecision], None] | None = None
    decisions: list[PolicyDecision] = field(default_factory=list)

    def create(self, job_id: str, agent_ids: list[str]) -> JobWorkspace:
        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
            raise ValueError("Unsafe job_id")
        root = self._within(self.runtime_root, self.runtime_root / job_id)
        workspace = JobWorkspace(
            job_id=job_id,
            root=root,
            shared_inputs=root / "shared_inputs",
            agents=root / "agents",
            staging=root / "staging",
            final=root / "final",
            task_state=root / "task_state",
        )
        for path in (workspace.shared_inputs, workspace.staging, workspace.final, workspace.task_state):
            path.mkdir(parents=True, exist_ok=True)
        for agent_id in agent_ids:
            workspace.scratch(agent_id).mkdir(parents=True, exist_ok=True)
            workspace.agent_staging(agent_id).mkdir(parents=True, exist_ok=True)
        return workspace

    @staticmethod
    def _within(root: Path, candidate: Path) -> Path:
        root_resolved = root.resolve()
        resolved = candidate.resolve(strict=False)
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise ValueError(f"PATH_ESCAPE: {candidate}")
        return resolved

    def authorize_path(self, *, workspace: JobWorkspace, policy: AgentCapabilityPolicy, agent_id: str, action: str, candidate: str | Path) -> Path:
        attempted = str(candidate)
        try:
            path = Path(candidate)
            if not path.is_absolute():
                path = workspace.root / path
            resolved = path.resolve(strict=False)
            protected = [PATHS.project_root / name for name in ("models", "config", ".git", "geoagent", "renewable_site_agent")]
            if action in {"write", "delete", "move", "mkdir"} and any(resolved == item.resolve() or item.resolve() in resolved.parents for item in protected):
                return self._deny(workspace.job_id, agent_id, action, attempted, "PROTECTED_PATH", "Configuration, model, VCS and source paths are immutable to agents")
            self._within(workspace.root, resolved)
            roots = policy.allowed_read_paths if action == "read" else policy.allowed_write_paths
            allowed = any(resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents for root in roots)
            if not allowed:
                code = "CROSS_AGENT_WRITE" if action != "read" and workspace.agents.resolve() in resolved.parents else ("DIRECT_FINAL_WRITE" if action != "read" and workspace.final.resolve() in resolved.parents else "WORKSPACE_POLICY_DENIED")
                return self._deny(workspace.job_id, agent_id, action, attempted, code, "Path is outside the agent capability roots")
            # Resolve each existing parent to catch symlink/junction escapes.
            parent = resolved.parent
            while parent != workspace.root.parent and parent != parent.parent:
                if parent.exists():
                    actual = parent.resolve(strict=True)
                    if actual != workspace.root and workspace.root not in actual.parents:
                        return self._deny(workspace.job_id, agent_id, action, attempted, "PATH_ESCAPE", "Symlink or junction escapes the job workspace")
                if parent == workspace.root:
                    break
                parent = parent.parent
            self._allow(workspace.job_id, agent_id, action, attempted, "Path authorized by workspace capability roots")
            return resolved
        except PolicyDenied:
            raise
        except Exception as exc:
            return self._deny(workspace.job_id, agent_id, action, attempted, "PATH_ESCAPE", str(exc))

    def authorize_tool(self, *, workspace: JobWorkspace, policy: AgentCapabilityPolicy, agent_id: str, tool_name: str, provider: str | None = None) -> None:
        if tool_name not in policy.allowed_tools:
            self._deny(workspace.job_id, agent_id, f"tool:{tool_name}", None, "UNAUTHORIZED_TOOL", "Tool is outside the agent allow-list")
        if provider and provider not in policy.allowed_providers:
            self._deny(workspace.job_id, agent_id, f"provider:{provider}", None, "UNAUTHORIZED_PROVIDER", "Provider is outside the agent allow-list")
        self._allow(workspace.job_id, agent_id, f"tool:{tool_name}", None, "Tool/provider authorized")

    def authorize_legacy_tool_call(
        self, *, workspace: JobWorkspace, policy: AgentCapabilityPolicy,
        agent_id: str, tool_name: str, arguments: dict[str, Any],
        requires_confirmation: bool = False, confirmed: bool = False,
    ) -> dict[str, Any]:
        """Unified non-prompt gate for broad/legacy tool invocations.

        Legacy tools are hidden by default because their names are absent from
        specialist policies.  Even an explicitly allow-listed wrapper must
        pass confirmation and path ownership checks before its implementation
        can run.
        """
        self.authorize_tool(
            workspace=workspace, policy=policy, agent_id=agent_id,
            tool_name=tool_name,
        )
        if requires_confirmation and not confirmed:
            self._deny(
                workspace.job_id, agent_id, f"legacy_tool:{tool_name}", None,
                "REQUIRES_CONFIRMATION", "Side-effecting legacy operation requires explicit confirmation",
            )
        path_keys = {
            "path", "file", "filename", "filepath", "file_path", "output",
            "output_path", "out_path", "save_path", "destination", "directory",
            "folder", "project_path", "raster", "vector",
        }
        governed = dict(arguments)
        for key, value in arguments.items():
            normalized = key.lower()
            if value is None or not isinstance(value, (str, Path)):
                continue
            if normalized in path_keys or normalized.endswith(("_path", "_file", "_dir", "_folder")):
                governed[key] = str(self.authorize_path(
                    workspace=workspace, policy=policy, agent_id=agent_id,
                    action="write", candidate=value,
                ))
        return governed

    def authorize_delegate(self, *, workspace: JobWorkspace, caller_id: str, caller_policy: AgentCapabilityPolicy, target_role: str) -> None:
        if not caller_policy.can_delegate or target_role not in caller_policy.allowed_agents:
            self._deny(workspace.job_id, caller_id, f"delegate:{target_role}", None, "DELEGATION_DENIED", "Only orchestrator may delegate specialist agents")
        self._allow(workspace.job_id, caller_id, f"delegate:{target_role}", None, "Delegation authorized")

    def write_bytes(self, *, workspace: JobWorkspace, policy: AgentCapabilityPolicy, agent_id: str, path: str | Path, data: bytes) -> Path:
        destination = self.authorize_path(workspace=workspace, policy=policy, agent_id=agent_id, action="write", candidate=path)
        with TELEMETRY.span("artifact.stage", job_id=workspace.job_id, agent_id=agent_id, workspace=str(destination.parent), artifact=str(destination)):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        return destination

    def _deny(self, job_id: str, agent_id: str, action: str, path: str | None, code: str, reason: str) -> Any:
        decision = PolicyDecision(allowed=False, error_code=code, agent_id=agent_id, job_id=job_id, attempted_action=action, path=path, reason=reason)
        self.decisions.append(decision)
        if self.audit_hook:
            self.audit_hook(decision)
        with TELEMETRY.span("workspace.deny", job_id=job_id, agent_id=agent_id, workspace=path, policy_decision=code, attempted_action=action):
            pass
        raise PolicyDenied(decision)

    def _allow(self, job_id: str, agent_id: str, action: str, path: str | None, reason: str) -> None:
        decision = PolicyDecision(allowed=True, agent_id=agent_id, job_id=job_id, attempted_action=action, path=path, reason=reason)
        self.decisions.append(decision)
        if self.audit_hook:
            self.audit_hook(decision)
        with TELEMETRY.span("workspace.authorize", job_id=job_id, agent_id=agent_id, workspace=path, policy_decision="ALLOW", attempted_action=action):
            pass


@dataclass
class ArtifactCommitGate:
    workspace_manager: WorkspaceManager
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    def commit(self, *, workspace: JobWorkspace, contract: GoalContract, artifact: ArtifactRef, allowed_types: tuple[str, ...], valid_dependency_ids: set[str], expected_version: str | None = None) -> ArtifactRef:
        source = Path(artifact.path).resolve(strict=False)
        expected_root = workspace.agent_staging(artifact.producer_agent).resolve()
        checks = {
            "job_id": artifact.job_id == workspace.job_id,
            "contract_hash": artifact.contract_hash in {None, contract.contract_hash},
            "type": artifact.artifact_type in allowed_types,
            "source_namespace": source == expected_root or expected_root in source.parents,
            "exists": source.is_file(),
            "dependencies": set(artifact.dependency_artifact_ids).issubset(valid_dependency_ids),
        }
        if source.is_file():
            checks["checksum"] = sha256_file(source) == artifact.checksum_sha256
        if not all(checks.values()):
            event = {"event": "COMMIT_REJECTED", "artifact_id": artifact.artifact_id, "job_id": workspace.job_id, "checks": checks}
            self.audit_events.append(event)
            raise PolicyDenied(PolicyDecision(allowed=False, error_code="COMMIT_REJECTED", agent_id=artifact.producer_agent, job_id=workspace.job_id, attempted_action="artifact.commit", path=str(source), reason=json.dumps(checks, sort_keys=True)))
        destination = workspace.final / artifact.artifact_type / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing_hash = sha256_file(destination)
            if expected_version is None or existing_hash != expected_version:
                self.audit_events.append({"event": "COMMIT_REJECTED", "reason": "version_conflict", "path": str(destination)})
                raise PolicyDenied(PolicyDecision(allowed=False, error_code="COMMIT_VERSION_CONFLICT", agent_id=artifact.producer_agent, job_id=workspace.job_id, attempted_action="artifact.commit", path=str(destination), reason="Final artifact already exists with a different or unversioned checksum"))
        # Staging and final are on the same job filesystem; replace is the
        # atomic promotion and intentionally removes the staging owner.
        os.replace(source, destination)
        committed = artifact.model_copy(update={"path": str(destination), "checksum_sha256": sha256_file(destination)})
        self.audit_events.append({"event": "artifact.commit", "artifact_id": artifact.artifact_id, "path": str(destination), "checksum": committed.checksum_sha256})
        return committed

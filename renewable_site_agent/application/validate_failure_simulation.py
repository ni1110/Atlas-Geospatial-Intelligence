"""Validate failure simulation without recursively calling the full control plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_agent_executor import validate_agent_executor
from renewable_site_agent.application.validate_agent_state_summary import (
    validate_agent_state_summary,
)


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"
DEFAULT_EXPECTED_RESUME_STEP = "prepare_terrain_dem"


class FailureSimulationValidationError(RuntimeError):
    """Raised when failure simulation is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FailureSimulationValidationError(f"Missing simulation file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_failure_simulation(
    job_id: str,
    expected_resume_step: str = DEFAULT_EXPECTED_RESUME_STEP,
) -> dict[str, Any]:
    simulation_dir = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "simulations"
    )

    simulated_executor_path = simulation_dir / "executor_run_failure_simulation_v1.json"
    simulated_policy_path = simulation_dir / "resume_policy_failure_simulation_v1.json"

    simulated_executor = read_json(simulated_executor_path)
    simulated_policy = read_json(simulated_policy_path)

    if simulated_executor.get("executor_status") != "failed":
        raise FailureSimulationValidationError(
            f"Expected simulated executor_status=failed, got {simulated_executor.get('executor_status')}"
        )

    summary = simulated_executor.get("summary", {})
    if summary.get("failed_step_count") != 1:
        raise FailureSimulationValidationError(
            f"Expected one simulated failed step, got {summary.get('failed_step_count')}"
        )

    failed_steps = [
        step
        for step in simulated_executor.get("steps", [])
        if step.get("status") == "failed"
    ]

    if len(failed_steps) != 1:
        raise FailureSimulationValidationError(
            f"Expected exactly one failed step, got {len(failed_steps)}"
        )

    failed_step = failed_steps[0]
    failed_tool_id = (
        failed_step.get("registered_tool_id")
        or failed_step.get("tool_id")
        or failed_step.get("step_id")
    )

    if failed_tool_id != expected_resume_step:
        raise FailureSimulationValidationError(
            f"Expected failed step {expected_resume_step}, got {failed_tool_id}"
        )

    artifact_checks = failed_step.get("artifact_checks", [])
    if not any(check.get("simulated_missing") is True for check in artifact_checks):
        raise FailureSimulationValidationError(
            "Failed step does not contain a simulated missing artifact."
        )

    if simulated_policy.get("policy_status") != "resume_after_failure_fix":
        raise FailureSimulationValidationError(
            f"Expected resume_after_failure_fix, got {simulated_policy.get('policy_status')}"
        )

    if simulated_policy.get("next_resume_step") != expected_resume_step:
        raise FailureSimulationValidationError(
            f"Expected next_resume_step={expected_resume_step}, got {simulated_policy.get('next_resume_step')}"
        )

    if simulated_policy.get("automatic_resume_allowed") is not False:
        raise FailureSimulationValidationError(
            "automatic_resume_allowed must be false for failed-artifact recovery."
        )

    # Non-recursive real-system safety check.
    real_executor = validate_agent_executor(job_id)
    real_state_summary = validate_agent_state_summary(job_id)

    if real_executor.get("agent_executor_validation_status") != "passed":
        raise FailureSimulationValidationError("Real executor validation failed.")

    if real_state_summary.get("agent_state_summary_validation_status") != "passed":
        raise FailureSimulationValidationError("Real state summary validation failed.")

    if real_executor.get("executor_status") != "completed":
        raise FailureSimulationValidationError(
            f"Real executor status was polluted: {real_executor.get('executor_status')}"
        )

    if real_executor.get("failed_step_count") != 0:
        raise FailureSimulationValidationError(
            f"Real executor has failed steps: {real_executor.get('failed_step_count')}"
        )

    if real_state_summary.get("missing_artifact_count") != 0:
        raise FailureSimulationValidationError(
            f"Real system has missing artifacts: {real_state_summary.get('missing_artifact_count')}"
        )

    return {
        "job_id": job_id,
        "failure_simulation_validation_status": "passed",
        "simulated_executor_path": str(simulated_executor_path),
        "simulated_resume_policy_path": str(simulated_policy_path),
        "simulated_executor_status": simulated_executor.get("executor_status"),
        "simulated_failed_step_count": summary.get("failed_step_count"),
        "simulated_failed_tool_id": failed_tool_id,
        "simulated_resume_policy_status": simulated_policy.get("policy_status"),
        "simulated_resume_policy_status_zh": simulated_policy.get("policy_status_zh"),
        "simulated_next_resume_step": simulated_policy.get("next_resume_step"),
        "simulated_automatic_resume_allowed": simulated_policy.get(
            "automatic_resume_allowed"
        ),
        "real_system_status_after_simulation": "passed",
        "real_executor_status_after_simulation": real_executor.get("executor_status"),
        "real_failed_step_count_after_simulation": real_executor.get("failed_step_count"),
        "real_missing_artifact_count_after_simulation": real_state_summary.get(
            "missing_artifact_count"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    parser.add_argument("--expected-resume-step", default=DEFAULT_EXPECTED_RESUME_STEP)
    args = parser.parse_args()

    result = validate_failure_simulation(args.job_id, args.expected_resume_step)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

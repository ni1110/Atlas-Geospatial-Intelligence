"""Regression tests for Closed Loop 3 final Agent control plane."""

from __future__ import annotations

from pathlib import Path

import pytest

from renewable_site_agent.application.validate_closed_loop_3_final import (
    validate_closed_loop_3_final,
)
from renewable_site_agent.application.validate_agent_control_plane import (
    validate_agent_control_plane,
)
from renewable_site_agent.application.validate_failure_simulation import (
    validate_failure_simulation,
)


JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"
pytestmark = pytest.mark.legacy_artifact


def test_closed_loop_3_final_validation_passes() -> None:
    result = validate_closed_loop_3_final(JOB_ID)

    assert result["closed_loop_3_final_validation_status"] == "passed"
    assert result["operational_status"] == "current_scope_completed"
    assert result["final_recommendation_allowed"] is True

    assert result["candidate_count"] == 608
    assert result["blocked_constraint_count"] == 0
    assert result["out_of_scope_constraint_count"] == 2

    assert result["completed_step_count"] == 13
    assert result["failed_step_count"] == 0
    assert result["pending_step_count"] == 0

    assert result["failure_simulation_status"] == "passed"
    assert result["dynamic_replanning_needed_now"] is False
    assert result["deprecated_artifacts_status"] == "passed"

    assert result["required_artifact_count"] == 14
    assert result["missing_artifact_count"] == 0


def test_agent_control_plane_contains_all_closed_loop_3_modules() -> None:
    result = validate_agent_control_plane(JOB_ID)

    expected_modules = {
        "available_data_job",
        "execution_plan",
        "agent_plan",
        "plan_run",
        "agent_executor",
        "scope_decision",
        "resume_policy",
        "failure_simulation",
        "dynamic_replanning_record",
        "deprecated_artifacts",
        "agent_state_summary",
    }

    assert result["agent_control_plane_validation_status"] == "passed"
    assert set(result["validation_statuses"]) == expected_modules
    assert all(status == "passed" for status in result["validation_statuses"].values())

    assert result["executor_status"] == "completed"
    assert result["resume_policy_status"] == "no_resume_needed_current_scope_completed"
    assert result["future_scope_change_policy"] == "create_new_plan_version_not_resume_current_run"
    assert result["old_blocked_decision_exists"] is False
    assert result["polluted_active_control_file_count"] == 0


def test_failure_simulation_triggers_resume_policy_without_polluting_real_run() -> None:
    result = validate_failure_simulation(
        JOB_ID,
        expected_resume_step="prepare_terrain_dem",
    )

    assert result["failure_simulation_validation_status"] == "passed"
    assert result["simulated_executor_status"] == "failed"
    assert result["simulated_failed_step_count"] == 1
    assert result["simulated_failed_tool_id"] == "prepare_terrain_dem"

    assert result["simulated_resume_policy_status"] == "resume_after_failure_fix"
    assert result["simulated_next_resume_step"] == "prepare_terrain_dem"
    assert result["simulated_automatic_resume_allowed"] is False

    assert result["real_system_status_after_simulation"] == "passed"
    assert result["real_executor_status_after_simulation"] == "completed"
    assert result["real_failed_step_count_after_simulation"] == 0
    assert result["real_missing_artifact_count_after_simulation"] == 0


def test_closed_loop_3_final_artifact_exists() -> None:
    final_path = (
        Path("runtime/jobs")
        / JOB_ID
        / "agent"
        / "closed_loop_3_final_validation_v1.json"
    )

    assert final_path.is_file()

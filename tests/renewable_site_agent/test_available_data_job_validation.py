"""Regression tests for the available-data GeoAgent site-selection result."""

from __future__ import annotations

from pathlib import Path

import pytest

from renewable_site_agent.application.create_job import RUNTIME_ROOT
from renewable_site_agent.application.validate_available_data_job import validate_job


JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


def test_available_data_job_validation_passes() -> None:
    """Validate the current available-data screening output end-to-end."""

    final_candidate_path = (
        RUNTIME_ROOT
        / "jobs"
        / JOB_ID
        / "final"
        / "available_data_screened_candidate_sites.geojson"
    )

    if not final_candidate_path.is_file():
        pytest.skip(
            "Available-data final candidate artifact is not present in this runtime."
        )

    result = validate_job(JOB_ID)

    assert result["validation_status"] == "passed"

    assert result["final_candidate_count"] == 608
    assert result["preview_candidate_count"] == 608

    assert result["total_area_km2"] == pytest.approx(
        6756.97215,
        abs=1e-5,
    )

    assert result["water_overlap_km2"] <= 0.01
    assert result["protected_overlap_km2"] <= 0.01

    assert result["preview_candidate_source_kind"] == "available_data_final"
    assert (
        result["preview_candidate_source_stage"]
        == "available_data_screened_preliminary_candidates"
    )
    assert (
        result["preview_candidate_screening_status"]
        == "preliminary_not_final_recommendation"
    )

    assert (
        result["skipped_constraints"]["max_distance_to_substation_km"]
        == "skipped_not_evaluated"
    )
    assert (
        result["skipped_constraints"]["exclude_prime_farmland"]
        == "skipped_not_evaluated"
    )

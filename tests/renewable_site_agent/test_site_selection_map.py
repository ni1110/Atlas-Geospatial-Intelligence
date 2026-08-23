"""Regression test for the persisted renewable-site map rendering tool.

This test uses the real persisted job, SQLite database, evidence files, and map
preview artifacts. Only the browser map object is replaced with a lightweight
fake so the rendering contract can be verified without starting Solara.
"""

from __future__ import annotations

import unittest
from typing import Any

import pytest

from renewable_site_agent.tools.site_selection_map import (
    BOUNDARY_PAINT,
    CANDIDATE_PAINT,
    WATER_PAINT,
    load_site_selection_layers,
)

pytestmark = pytest.mark.legacy_artifact


JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class FakeMap:
    """Minimal AnyMap-compatible object used for deterministic verification."""

    def __init__(self) -> None:
        self.layers: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.bounds: list[list[float]] | None = None

    def add_geojson(self, data: dict[str, Any], **kwargs: Any) -> None:
        call = {
            "name": kwargs["name"],
            "layer_type": kwargs["layer_type"],
            "paint": kwargs["paint"],
            "feature_count": len(data["features"]),
            "fit_bounds": kwargs["fit_bounds"],
            "visible": kwargs["visible"],
            "opacity": kwargs["opacity"],
        }
        self.calls.append(call)
        self.layers.append({"name": kwargs["name"]})

    def fit_bounds(self, bounds: list[list[float]]) -> None:
        self.bounds = bounds


class SiteSelectionMapRegressionTest(unittest.TestCase):
    def test_real_job_preview_contract_and_idempotency(self) -> None:
        map_obj = FakeMap()

        first = load_site_selection_layers(map_obj, JOB_ID)

        self.assertTrue(first["success"])
        self.assertEqual(first["job_status"], "candidates_ready")

        self.assertEqual(first["boundary_feature_count"], 1)
        self.assertEqual(first["water_feature_count"], 3587)
        self.assertEqual(first["candidate_feature_count"], 625)
        self.assertEqual(first["database_candidate_count"], 625)

        self.assertEqual(first["preview_boundary_feature_count"], 1)
        self.assertEqual(first["preview_water_feature_count"], 1)
        self.assertEqual(first["preview_candidate_feature_count"], 625)
        self.assertGreater(first["preview_reduction_percent"], 0)

        self.assertEqual(len(map_obj.calls), 3)
        self.assertEqual(
            [call["layer_type"] for call in map_obj.calls],
            ["fill", "fill", "line"],
        )
        self.assertEqual(
            [call["feature_count"] for call in map_obj.calls],
            [625, 1, 1],
        )
        self.assertEqual(map_obj.calls[0]["paint"], CANDIDATE_PAINT)
        self.assertEqual(map_obj.calls[1]["paint"], WATER_PAINT)
        self.assertEqual(map_obj.calls[2]["paint"], BOUNDARY_PAINT)
        self.assertTrue(all(call["fit_bounds"] is False for call in map_obj.calls))
        self.assertTrue(all(call["visible"] is True for call in map_obj.calls))
        self.assertTrue(all(call["opacity"] == 1.0 for call in map_obj.calls))

        self.assertIsNotNone(map_obj.bounds)
        expected_bounds = [
            [115.54020466000009, 39.57509420400004],
            [117.50647273100003, 41.07550586600007],
        ]
        for actual_pair, expected_pair in zip(map_obj.bounds, expected_bounds):
            for actual, expected in zip(actual_pair, expected_pair):
                self.assertAlmostEqual(actual, expected, places=8)

        calls_after_first_run = len(map_obj.calls)
        second = load_site_selection_layers(map_obj, JOB_ID)

        self.assertEqual(len(map_obj.calls), calls_after_first_run)
        self.assertEqual(second["loaded_layers"], [])
        self.assertEqual(len(second["skipped_existing_layers"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

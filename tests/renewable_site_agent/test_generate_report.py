"""Regression test for deterministic site-selection report generation."""

from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path

import pytest

from renewable_site_agent.application.generate_report import generate_site_selection_report


JOB_ID = os.environ.get(
    "TEST_SITE_SELECTION_JOB_ID",
    "ce5f4714-b004-435f-9876-49c99e8966ff",
)
pytestmark = pytest.mark.legacy_artifact


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class GenerateReportRegressionTest(unittest.TestCase):
    def test_real_job_report_is_traceable_and_deterministic(self) -> None:
        first = generate_site_selection_report(JOB_ID)
        first_hash = sha256_file(first.report_path)
        first_bytes = first.report_path.read_bytes()

        second = generate_site_selection_report(JOB_ID)
        second_hash = sha256_file(second.report_path)
        second_bytes = second.report_path.read_bytes()

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first.candidate_count, 625)
        self.assertEqual(first.report_sha256, first_hash)
        self.assertTrue(first.metadata_path.is_file())

        html = first_bytes.decode("utf-8")
        self.assertIn(JOB_ID, html)
        self.assertIn("625", html)
        self.assertIn("not approved, qualified, or recommended", html)
        self.assertIn("geoBoundaries", html)
        self.assertIn("OpenStreetMap", html)
        self.assertIn("<svg", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)

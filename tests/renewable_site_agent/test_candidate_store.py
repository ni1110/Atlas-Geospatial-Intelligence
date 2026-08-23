"""Unit tests for candidate-site SQLite persistence."""

import tempfile
import unittest
from pathlib import Path

from renewable_site_agent.domain.candidates import CandidateSite
from renewable_site_agent.domain.jobs import SiteSelectionJob
from renewable_site_agent.infrastructure.candidate_store import (
    SQLiteCandidateStore,
)
from renewable_site_agent.infrastructure.job_store import (
    SQLiteJobStore,
)
from renewable_site_agent.config import PATHS


class CandidateStoreTest(unittest.TestCase):
    def test_replace_is_idempotent_and_removes_stale_rows(
        self,
    ) -> None:
        test_temp = PATHS.temp / "legacy-candidate-store-tests"
        test_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as temporary_directory:
            database_path = (
                Path(temporary_directory) / "jobs.db"
            )

            job_store = SQLiteJobStore(database_path)
            candidate_store = SQLiteCandidateStore(
                database_path
            )

            job = SiteSelectionJob(
                request_text="测试候选地持久化"
            )

            job_store.save(
                job,
                event_type="job_received",
            )

            candidate_a = CandidateSite(
                candidate_id="candidate-a",
                job_id=job.job_id,
                rank=1,
                area_km2=100.0,
                centroid_lon=116.4,
                centroid_lat=39.9,
                geometry_sha256="a" * 64,
                artifact_path="runtime/candidates.geojson",
                source_evidence_id="evidence-a",
            )

            candidate_b = CandidateSite(
                candidate_id="candidate-b",
                job_id=job.job_id,
                rank=2,
                area_km2=50.0,
                centroid_lon=116.6,
                centroid_lat=40.0,
                geometry_sha256="b" * 64,
                artifact_path="runtime/candidates.geojson",
                source_evidence_id="evidence-a",
            )

            saved_count = candidate_store.replace_for_job(
                job.job_id,
                [candidate_a, candidate_b],
            )

            self.assertEqual(saved_count, 2)
            self.assertEqual(
                candidate_store.count_by_job(job.job_id),
                2,
            )

            # Re-running the same operation must not duplicate rows.
            candidate_store.replace_for_job(
                job.job_id,
                [candidate_a, candidate_b],
            )

            self.assertEqual(
                candidate_store.count_by_job(job.job_id),
                2,
            )

            # A new candidate generation replaces stale candidates.
            updated_candidate_a = candidate_a.model_copy(
                update={
                    "area_km2": 110.0,
                    "rank": 1,
                }
            )

            candidate_store.replace_for_job(
                job.job_id,
                [updated_candidate_a],
            )

            restored = candidate_store.list_by_job(
                job.job_id
            )

            self.assertEqual(len(restored), 1)
            self.assertEqual(
                restored[0].candidate_id,
                "candidate-a",
            )
            self.assertEqual(
                restored[0].area_km2,
                110.0,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

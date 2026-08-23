from renewable_site_agent.domain.jobs import (
    JobStatus,
    SiteSelectionJob,
)
from renewable_site_agent.infrastructure.job_store import (
    SQLiteJobStore,
)


def test_job_state_and_persistence(tmp_path):
    store = SQLiteJobStore(
        tmp_path / "jobs.db"
    )

    job = SiteSelectionJob(
        request_text="测试100MW光伏项目"
    )

    store.save(
        job,
        event_type="job_received",
    )

    job.transition(JobStatus.PARSING)

    store.save(
        job,
        event_type="constraint_parsing_started",
    )

    restored = store.get(job.job_id)

    assert restored.job_id == job.job_id
    assert restored.status == JobStatus.PARSING

    events = store.list_events(job.job_id)

    assert [
        event["event_type"]
        for event in events
    ] == [
        "job_received",
        "constraint_parsing_started",
    ]

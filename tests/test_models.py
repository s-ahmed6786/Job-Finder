from datetime import datetime, timedelta, timezone

import pytest

from ingest.adapters import JobDetail, JobSummary


def test_summary_accepts_utc():
    s = JobSummary("123", "Engineer", datetime(2026, 11, 1, tzinfo=timezone.utc), True)
    assert s.posted_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "bad",
    [datetime(2026, 11, 1), datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=-5)))],
    ids=["naive", "minus-05:00"],
)
def test_summary_rejects_non_utc(bad):
    with pytest.raises(ValueError):
        JobSummary("123", "Engineer", bad, True)


def test_detail_requires_url_and_id():
    kw = dict(title="t", posted_at=None, locations=None, locations_list=(),
              description_html=None, raw_payload={})
    with pytest.raises(ValueError):
        JobDetail(source_job_id="1", url="", **kw)
    with pytest.raises(ValueError):
        JobDetail(source_job_id="", url="https://x", **kw)

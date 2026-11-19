"""The contract between the poll loop and ATS adapters.

One module per ATS implements `Adapter`. The poll loop only sees these types;
it knows nothing ATS-specific. Adapters:

- parse defensively and raise `SchemaError` on unexpected response shapes;
- raise `BoardNotFound` on 404 for a list call (never return [] for it);
- raise `RateLimited` on 429 so the caller can back off and log;
- return timezone-aware UTC datetimes only (enforced below);
- strip personal data (recruiter/owner names, emails) from `raw_payload`;
- take URLs from the response, never construct them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

import httpx


# --- errors ------------------------------------------------------------------

class AdapterError(Exception):
    """Base class. Any of these must be logged loudly and must not change job status."""

    def __init__(self, message: str, *, company: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.company = company
        self.status_code = status_code


class BoardNotFound(AdapterError):
    """List call returned 404: bad board token / tenant, or the company left the ATS."""


class RateLimited(AdapterError):
    """HTTP 429. `retry_after` in seconds if the server sent one."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class SchemaError(AdapterError):
    """Response did not have the shape we expect (undocumented APIs change)."""


class ConfigError(AdapterError):
    """companies.config is missing or has invalid keys for this ATS."""


# --- data --------------------------------------------------------------------

def _require_utc(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC, got {value!r}")


@dataclass(frozen=True, slots=True)
class Company:
    id: int
    name: str
    ats: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JobSummary:
    """One row from a list call. Enough to dedupe and apply the 14-day window."""

    source_job_id: str
    title: str
    # Best known posting time. Exact for Greenhouse (first_published); approximate
    # for Workday lists ("Posted 5 Days Ago"), see `posted_at_exact`.
    posted_at: datetime | None
    posted_at_exact: bool
    url: str | None = None
    locations: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        _require_utc("posted_at", self.posted_at)
        if not self.source_job_id:
            raise ValueError("source_job_id is required")


@dataclass(frozen=True, slots=True)
class JobDetail:
    """Everything needed to write a `jobs` row, except cleaning and hashing,
    which the poll loop does the same way for every ATS."""

    source_job_id: str
    title: str
    url: str
    posted_at: datetime | None
    locations: str | None
    locations_list: tuple[str, ...]
    description_html: str | None      # HTML, already unescaped once
    raw_payload: dict[str, Any]       # personal data already removed
    role_key: str | None = None
    req_id: str | None = None

    def __post_init__(self) -> None:
        _require_utc("posted_at", self.posted_at)
        if not self.source_job_id:
            raise ValueError("source_job_id is required")
        if not self.url:
            raise ValueError("url is required")


# --- adapter -----------------------------------------------------------------

class Adapter(ABC):
    """Base for ATS adapters. The poll loop owns the HTTP client (User-Agent,
    timeouts, pacing) and passes it in."""

    ats: ClassVar[str]

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @abstractmethod
    async def list_jobs(self, company: Company, *, since: datetime) -> list[JobSummary]:
        """Jobs posted at or after `since` (UTC), newest first where the ATS allows.

        Workday: stops paging at the first job older than `since`. Greenhouse:
        one call, then filtered. Raises AdapterError subclasses; never returns
        [] to mean "error"."""

    @abstractmethod
    async def get_job(self, company: Company, source_job_id: str) -> JobDetail:
        """Full detail for one job. Called only for jobs new to the DB."""

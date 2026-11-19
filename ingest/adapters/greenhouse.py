"""Greenhouse Job Board API adapter.

Config (companies.config):
    {"board_token": "databricks"}

List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs      (whole board, no paging)
Detail: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id} (adds HTML-escaped `content`)

EU-hosted boards (e.g. IMC, whose job pages live on job-boards.eu.greenhouse.io)
are served by the same API host; verified live Nov 2026. There is no
boards-api.eu.greenhouse.io (DNS does not resolve).
"""

from __future__ import annotations

import copy
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import register
from .base import (
    Adapter,
    BoardNotFound,
    Company,
    ConfigError,
    EmptyBoard,
    JobDetail,
    JobNotFound,
    JobSummary,
    SchemaError,
)

log = logging.getLogger(__name__)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_RE = re.compile(r"^[0-9]+$")


@register
class GreenhouseAdapter(Adapter):
    ats = "greenhouse"

    # --- public contract -----------------------------------------------------

    async def list_jobs(self, company: Company, *, since: datetime) -> list[JobSummary]:
        url = f"{self._base(company)}/jobs"
        resp = await self._send(company, "GET", url)
        if resp.status_code == 404:
            raise BoardNotFound(f"board not found: {url}", company=company.name, status_code=404)
        data = self._json(company, resp)

        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SchemaError(f"list response has no 'jobs' array: {url}", company=company.name)
        jobs = data["jobs"]
        if not jobs:
            raise EmptyBoard(f"board returned zero jobs: {url}", company=company.name)

        total = (data.get("meta") or {}).get("total")
        if isinstance(total, int) and total != len(jobs):
            log.warning("%s: meta.total=%s but %d jobs returned", company.name, total, len(jobs))

        out: list[JobSummary] = []
        bad = undated = 0
        for item in jobs:
            try:
                summary = _summary(item)
            except (SchemaError, ValueError, TypeError) as e:
                bad += 1
                log.error("%s: skipping malformed job %r: %s", company.name, _id_of(item), e)
                continue
            if summary.posted_at is None:
                undated += 1
                continue
            if summary.posted_at >= since:
                out.append(summary)

        if bad == len(jobs):
            raise SchemaError(f"every job on the board was malformed: {url}", company=company.name)
        if undated:
            # No first_published: can't place them in the window, so leave them out.
            log.warning("%s: %d jobs without first_published were skipped", company.name, undated)

        out.sort(key=lambda s: s.posted_at, reverse=True)
        return out

    async def get_job(self, company: Company, source_job_id: str) -> JobDetail:
        if not _ID_RE.match(source_job_id):
            raise ValueError(f"Greenhouse job ids are numeric, got {source_job_id!r}")
        url = f"{self._base(company)}/jobs/{source_job_id}"
        resp = await self._send(company, "GET", url)
        if resp.status_code == 404:
            raise JobNotFound(f"job not found: {url}", company=company.name, status_code=404)
        data = self._json(company, resp)
        if not isinstance(data, dict):
            raise SchemaError(f"detail response is not an object: {url}", company=company.name)
        try:
            detail = _detail(data)
        except (ValueError, TypeError) as e:
            raise SchemaError(f"unexpected detail response {url}: {e}", company=company.name) from e
        if detail.source_job_id != source_job_id:
            raise SchemaError(
                f"asked for job {source_job_id}, got {detail.source_job_id}", company=company.name
            )
        return detail

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _base(company: Company) -> str:
        token = company.config.get("board_token")
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            raise ConfigError(f"invalid or missing board_token: {token!r}", company=company.name)
        return f"{API_BASE}/{token}"


# --- parsing (pure functions, easy to test) ------------------------------------

def _id_of(item: Any) -> Any:
    return item.get("id") if isinstance(item, dict) else None


def _job_id(item: dict[str, Any]) -> str:
    job_id = item.get("id")
    if isinstance(job_id, bool) or not isinstance(job_id, int):
        raise SchemaError(f"job id is not an integer: {job_id!r}")
    return str(job_id)


def _str_field(item: dict[str, Any], key: str, *, required: bool) -> str | None:
    value = item.get(key)
    if value is None or value == "":
        if required:
            raise SchemaError(f"missing {key!r}")
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{key!r} is not a string: {value!r}")
    return value


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 with offset (e.g. '2026-03-20T16:25:04-04:00') -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SchemaError(f"timestamp is not a string: {value!r}")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise SchemaError(f"timestamp has no offset: {value!r}")
    return dt.astimezone(timezone.utc)


def _location(item: dict[str, Any]) -> str | None:
    loc = item.get("location")
    if loc is None:
        return None
    if not isinstance(loc, dict):
        raise SchemaError(f"location is not an object: {loc!r}")
    name = loc.get("name")
    return name if isinstance(name, str) and name.strip() else None


def split_locations(raw: str | None) -> tuple[str, ...]:
    """Split on ';', trim, collapse inner whitespace, drop empties. Commas are
    not separators: they also appear inside one place ("Chicago, Illinois")."""
    if not raw:
        return ()
    parts = (" ".join(p.split()) for p in raw.split(";"))
    return tuple(p for p in parts if p)


def _optional_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SchemaError(f"id field has unexpected type: {value!r}")
    return str(value)


def scrub_personal_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy without personal data: metadata of type 'user' (e.g.
    recruiter/owner name + email) is blanked, and any 'email' key is removed."""
    clean = copy.deepcopy(payload)
    for m in clean.get("metadata") or []:
        if isinstance(m, dict) and m.get("value_type") == "user":
            m["value"] = None
    return _drop_email_keys(clean)


def _drop_email_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_email_keys(v) for k, v in obj.items() if k.lower() != "email"}
    if isinstance(obj, list):
        return [_drop_email_keys(v) for v in obj]
    return obj


def _summary(item: Any) -> JobSummary:
    if not isinstance(item, dict):
        raise SchemaError(f"job entry is not an object: {item!r}")
    return JobSummary(
        source_job_id=_job_id(item),
        title=_str_field(item, "title", required=True),
        posted_at=parse_time(item.get("first_published")),
        posted_at_exact=True,
        url=_str_field(item, "absolute_url", required=True),
        locations=_location(item),
        raw=item,
    )


def _detail(item: dict[str, Any]) -> JobDetail:
    job_id = _job_id(item)
    content = _str_field(item, "content", required=False)
    locations = _location(item)
    return JobDetail(
        source_job_id=job_id,
        title=_str_field(item, "title", required=True),
        url=_str_field(item, "absolute_url", required=True),
        posted_at=parse_time(item.get("first_published")),
        locations=locations,
        locations_list=split_locations(locations),
        # Greenhouse HTML-escapes content once (&lt;p&gt;). Unescape exactly once.
        description_html=html.unescape(content) if content is not None else None,
        raw_payload=scrub_personal_data(item),
        role_key=_optional_id(item.get("internal_job_id")) or job_id,
        req_id=_optional_id(item.get("requisition_id")),
    )

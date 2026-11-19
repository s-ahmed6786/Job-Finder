"""Greenhouse adapter tests against saved real responses. No network: every
request goes to httpx.MockTransport, which serves fixtures by URL path."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ingest.adapters import (
    AdapterError, BoardNotFound, Company, ConfigError, EmptyBoard, JobNotFound,
    RateLimited, SchemaError, adapter_for,
)
from ingest.adapters.greenhouse import GreenhouseAdapter, split_locations

FIX = Path(__file__).parent / "fixtures" / "greenhouse"
SINCE = datetime(2026, 11, 2, tzinfo=timezone.utc)  # fixtures were captured ~Nov 16


def load(name):
    return json.loads((FIX / name).read_text())


def company(token="databricks", **extra):
    return Company(id=1, name=token, ats="greenhouse", config={"board_token": token, **extra})


def run(routes, coro_fn):
    """routes: {path: (status, json_body | str | None, headers)}. Records requested URLs."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status, body, headers = routes.get(request.url.path, (404, load("bad_token_404.json"), {}))
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await coro_fn(GreenhouseAdapter(client))

    return asyncio.run(go()), seen


def list_route(token, fixture):
    return {f"/v1/boards/{token}/jobs": (200, load(fixture), {})}


# --- list --------------------------------------------------------------------

@pytest.mark.parametrize("token,fixture,expected", [
    ("databricks", "databricks_list.json", 85),
    ("stripe", "stripe_list.json", 142),
    ("hubspotjobs", "hubspot_list.json", 21),
    ("imc", "imc_list.json", 10),
    ("optiverprivate", "optiver_private_list_content.json", 0),
])
def test_list_window(token, fixture, expected):
    jobs, seen = run(list_route(token, fixture), lambda a: a.list_jobs(company(token), since=SINCE))
    assert len(jobs) == expected
    assert seen == [f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"]  # one call, no paging
    assert all(j.posted_at >= SINCE and j.posted_at.tzinfo == timezone.utc for j in jobs)
    assert [j.posted_at for j in jobs] == sorted((j.posted_at for j in jobs), reverse=True)


def test_list_normalizes_offsets_to_utc():
    jobs, _ = run(list_route("imc", "imc_list.json"), lambda a: a.list_jobs(company("imc"), since=SINCE))
    job = next(j for j in jobs if j.source_job_id == "5036428101")
    # first_published 2026-11-15T22:27:27-05:00
    assert job.posted_at == datetime(2026, 11, 16, 3, 27, 27, tzinfo=timezone.utc)
    assert job.url.startswith("https://job-boards.eu.greenhouse.io/")  # taken from the response


def test_bad_token_is_board_not_found_not_empty():
    with pytest.raises(BoardNotFound) as e:
        run({}, lambda a: a.list_jobs(company("nope"), since=SINCE))
    assert e.value.status_code == 404


def test_empty_board_raises():
    routes = {"/v1/boards/x/jobs": (200, {"jobs": [], "meta": {"total": 0}}, {})}
    with pytest.raises(EmptyBoard):
        run(routes, lambda a: a.list_jobs(company("x"), since=SINCE))


def test_rate_limited():
    routes = {"/v1/boards/x/jobs": (429, {}, {"Retry-After": "30"})}
    with pytest.raises(RateLimited) as e:
        run(routes, lambda a: a.list_jobs(company("x"), since=SINCE))
    assert e.value.retry_after == 30


@pytest.mark.parametrize("status,body,exc", [
    (500, {"error": "boom"}, AdapterError),
    (200, "<html>not json</html>", SchemaError),
    (200, {"unexpected": True}, SchemaError),
    (200, {"jobs": [{"title": "no id"}, "junk"]}, SchemaError),  # every job malformed
])
def test_list_errors(status, body, exc):
    routes = {"/v1/boards/x/jobs": (status, body, {})}
    with pytest.raises(exc):
        run(routes, lambda a: a.list_jobs(company("x"), since=SINCE))


def test_one_malformed_job_is_skipped_not_fatal(caplog):
    good = load("imc_list.json")["jobs"][0]
    routes = {"/v1/boards/x/jobs": (200, {"jobs": [good, {"id": "oops"}]}, {})}
    jobs, _ = run(routes, lambda a: a.list_jobs(company("x"), since=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert [j.source_job_id for j in jobs] == [str(good["id"])]
    assert "malformed" in caplog.text


def test_network_error_is_adapter_error():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await GreenhouseAdapter(client).list_jobs(company("x"), since=SINCE)

    with pytest.raises(AdapterError):
        asyncio.run(go())


# --- detail ------------------------------------------------------------------

def test_detail_databricks():
    routes = {"/v1/boards/databricks/jobs/8472398002": (200, load("databricks_job_detail_8472398002.json"), {})}
    d, _ = run(routes, lambda a: a.get_job(company(), "8472398002"))
    assert d.source_job_id == "8472398002"
    assert d.title == "Engineering Manager - Databricks SQL Control Plane"
    assert d.url == "https://databricks.com/company/careers/open-positions/job?gh_jid=8472398002"
    assert d.posted_at == datetime(2026, 3, 20, 20, 25, 4, tzinfo=timezone.utc)  # -04:00 -> UTC
    assert d.role_key == "6383847002"  # internal_job_id
    assert d.req_id == "RDQ326R233"
    assert d.locations_list == ("Mountain View, California",)
    assert d.description_html.startswith("<p>RDQ326R233</p>")  # unescaped once
    assert "&lt;" not in d.description_html


def _detail_from_list(fixture, job_id, content="&lt;p&gt;hi&lt;/p&gt;"):
    item = next(j for j in load(fixture)["jobs"] if j["id"] == job_id)
    return {**item, "content": content}


def test_detail_role_key_falls_back_to_id_and_cleans_locations():
    body = _detail_from_list("imc_list.json", 5036428101)
    d, _ = run({"/v1/boards/imc/jobs/5036428101": (200, body, {})}, lambda a: a.get_job(company("imc"), "5036428101"))
    assert d.role_key == "5036428101"           # internal_job_id is null
    assert d.locations == "Sydney,  Australia"  # raw kept
    assert d.locations_list == ("Sydney, Australia",)


def test_detail_splits_semicolon_locations():
    body = _detail_from_list("databricks_list.json", 6918763002)
    d, _ = run({"/v1/boards/databricks/jobs/6918763002": (200, body, {})}, lambda a: a.get_job(company(), "6918763002"))
    assert d.locations_list == ("Bengaluru, India", "Mumbai, India")


def test_detail_strips_personal_data():
    body = _detail_from_list("optiver_private_list_content.json", 4855624002)
    assert "email" in json.dumps(body)  # precondition: fixture has a (redacted) owner
    d, _ = run({"/v1/boards/optiverprivate/jobs/4855624002": (200, body, {})},
               lambda a: a.get_job(company("optiverprivate"), "4855624002"))
    dumped = json.dumps(d.raw_payload)
    assert "email" not in dumped and "REDACTED" not in dumped
    owner = next(m for m in d.raw_payload["metadata"] if m["value_type"] == "user")
    assert owner["value"] is None and owner["name"] == "Global - Leadership Owner"
    assert body["metadata"] != d.raw_payload["metadata"]  # original not mutated


def test_detail_404_is_job_not_found():
    with pytest.raises(JobNotFound):
        run({}, lambda a: a.get_job(company(), "123"))


def test_detail_id_mismatch_is_schema_error():
    body = load("databricks_job_detail_8472398002.json")
    with pytest.raises(SchemaError):
        run({"/v1/boards/databricks/jobs/1": (200, body, {})}, lambda a: a.get_job(company(), "1"))


def test_detail_rejects_non_numeric_id():
    with pytest.raises(ValueError):
        run({}, lambda a: a.get_job(company(), "../../etc"))


# --- config & registry -------------------------------------------------------

@pytest.mark.parametrize("config", [{}, {"board_token": "a/b"}, {"board_token": 123}])
def test_bad_config(config):
    c = Company(id=1, name="x", ats="greenhouse", config=config)
    with pytest.raises(ConfigError):
        run({}, lambda a: a.list_jobs(c, since=SINCE))


def test_registered():
    assert adapter_for("greenhouse") is GreenhouseAdapter


@pytest.mark.parametrize("raw,expected", [
    (None, ()), ("N/A", ("N/A",)), ("US-Remote", ("US-Remote",)),
    ("NYC;  Chicago ; ;", ("NYC", "Chicago")), ("United States ", ("United States",)),
])
def test_split_locations(raw, expected):
    assert split_locations(raw) == expected

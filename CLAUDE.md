# CLAUDE.md — JobSeeker (placeholder name)

## What this is
A job-posting database fed by frequent polling of company career systems (ATS), read directly from the source. Long term it becomes a search/alert product for any profession (CS first, later teachers, healthcare, etc.). Do not hard-code anything CS-specific into the core schema.

## MVP scope (current)
**Only populate the database.** Poll a configured list of companies every 30 minutes and store their postings in Postgres.

Explicitly NOT in the MVP: email/alerts, user accounts, subscriptions, a search API or UI, AI-generated adapters, enrichment fields (YOE, salary, level).
Keep the design ready for them (see "Later"), but do not build them yet.

## Stack
- Python 3.12+, async (`httpx` + `asyncio`), Postgres. Plain SQL migrations or Alembic; keep it simple.
- Poller is a standalone process/cron job. A future user-facing API will be a separate process that only reads the same DB.
- Config per company in the DB (`companies.config` JSON) or a YAML file, not in code.
- Secrets (DB URL, future email keys) only via environment variables. Never in code, logs, fixtures or commits. Never accept or reproduce session cookies or tokens.

## Architecture
```
scheduler (every 30 min) -> for each active company -> ATS adapter -> normalized jobs
  -> dedupe vs DB (company_id + source_job_id) -> new & within window: fetch detail
  -> clean text, content_hash, keep raw_payload -> upsert into jobs
daily job: set status='expired' where posted_at < now() - 30 days
```
Adapter contract (one module per ATS; the poll loop knows nothing ATS-specific):
- `list_jobs(company) -> list[JobSummary]`
- `get_job(company, source_job_id) -> JobDetail`

## Data model (MVP)
`companies`: id, name, ats ('workday' | 'greenhouse'), config (JSON), active.
`jobs`: id, company_id, source_job_id (unique per company), role_key, req_id (display only), title, locations (raw string), locations_list, url, posted_at (UTC), description_text, content_hash, raw_payload (JSONB), first_seen_at, last_seen_at, status ('open' | 'expired').

Rules:
- Key a job by `(company_id, source_job_id)`. Never key on req_id (null, repeated, or "See Opening ID" in the wild).
- `role_key` = Greenhouse `internal_job_id` if present, else `id`. Used to group reposts / per-office duplicates later.
- Store everything in UTC. Greenhouse returns mixed -04:00/-05:00 offsets; normalize on ingest.
- Only write when a job is new or its `content_hash` changed. Do NOT update `last_seen_at` for every job on every poll.
- Keep `raw_payload` so new fields can be derived later without re-fetching.
- Do not store personal data (e.g. recruiter/owner names and emails in Greenhouse metadata).

## Polling rules
- **Window:** only consider jobs posted within the last 14 days, on the first run and on every run. (Otherwise old jobs look new later.)
- **Expiry:** a job lives 30 days from `posted_at` (fallback `first_seen_at`), then `status='expired'`. Soft-expire; keep the row. No closed-job detection in the MVP.
- **Workday recheck:** fetch page 1 (limit 20, newest first). Fetch the next page only if every job on the page was new. Stop at the first job older than the window.
- **Greenhouse recheck:** one call returns the whole board; filter by `first_published` within the window.
- **Detail calls only for new jobs.**
- **Errors:** an HTTP error, 404 or zero jobs from a company must raise an alert/log loudly and must not change any job's status. Never fail silently.
- **Be polite:** identifying User-Agent, spread requests across the 30-minute interval, back off on 429 and log it. Review each service's terms before scaling.

## Workday (CxS API) — verified on Cboe and GM
- List: `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`, body `{"appliedFacets":{}, "limit":20, "offset":0, "searchText":""}`.
- `limit` above 20 returns HTTP 400 with an empty message. Page with `offset`.
- `total` and `facets` are only reliable on the first page (offset 0).
- Detail: `GET .../{site}/job/{externalPath}`. The detail `startDate` is the real posting date; list dates are relative ("Posted Today", "Posted 5 Days Ago", "Posted 30+ Days Ago").
- Use `externalPath` as `source_job_id`. Requisition IDs repeat with -1/-2 suffixes and slugs do not follow title edits.
- `bulletFields`: only element 0 (the requisition id) is trustworthy; the rest varies per company.
- Multi-location jobs show "N Locations" in the list; real locations come from the detail call.
- Facet ids/names (e.g. job category) are per company. Any category filter must be matched by name per company, and must be a per-subscription option, not a default.
- Description is messy HTML (salary, visa, stack as free text). Unescape once, strip tags.
- Newest-first ordering was observed on Cboe only. Verify on other tenants.
- The endpoint is undocumented and may change: parse defensively and alert on schema surprises.

## Greenhouse (Job Board API) — verified on Databricks, Stripe, HubSpot, IMC, Optiver (private board)
- List: `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs` returns the whole board plus `meta.total`. No paging.
- `?content=true` adds `content` (HTML-escaped), `departments`, `offices`. Only use it on small boards. Databricks (892 jobs) and Stripe (791) are large; fetch details for new jobs instead.
- Detail: `GET /v1/boards/{token}/jobs/{id}` has the same fields plus content. `id` equals the `gh_jid` in `absolute_url`.
- Wrong board token returns HTTP 404 `{"status":404,"error":"Job not found"}`. Key on the status code, not the text. 404 on a list call = bad token / company left; do not treat as "zero jobs".
- Board token is not always the company name (HubSpot is `hubspotjobs`).
- `absolute_url` can be a company-hosted page (Databricks) or an EU host (IMC). Use the URL from the response; never construct it.
- `updated_at` is unreliable: bulk-synced on Databricks, IMC and partly Stripe. Detect edits with `content_hash`.
- `requisition_id` is unreliable (Stripe "See Opening ID", nulls on HubSpot/IMC/Optiver). `internal_job_id` is sometimes null and sometimes shared by many postings (IMC posts one role per office).
- Locations are free text: "N/A", "US-Remote", multi-city strings joined by `;`, double spaces. Keep the raw string and split on `;` and trim for the list.
- `language` is unreliable. Metadata differs per company (HubSpot has a structured salary object `Compensation Range (Job Posts)` with unit/min_value/max_value; Databricks has category).
- Pay in descriptions appears as nested spans (Databricks) or plain text (Optiver): strip tags first, then extract. There can be several pay zones per job.
- Text matching (needed later): unescape HTML once, strip tags, then match. For C# use `(?<![A-Za-z0-9])c#(?![A-Za-z0-9+])` because `\b` fails on `#`; treat `.NET` similarly.

## Companies (initial config)
| Company | ATS | Config | Status |
|---|---|---|---|
| Cboe | Workday | tenant `cboe`, wd1, site `External_Career_CBOE` | verified |
| GM | Workday | tenant `generalmotors`, wd5, site `Careers_GM` | verified |
| Databricks | Greenhouse | `databricks` | verified (892 jobs) |
| Stripe | Greenhouse | `stripe` | verified (791 jobs) |
| HubSpot | Greenhouse | `hubspotjobs` | verified; board has almost no engineering roles, check HubSpot's careers page |
| IMC | Greenhouse | `imc` (board is EU-hosted) | verified; unsure whether US API host also serves it |
| Optiver | Greenhouse? | token unknown (`optiverprivate` is a junk board) | find real token from an apply link |
| Microsoft, Google, Salesforce, ServiceNow, Bloomberg, Capital One, JPMorgan, Discover, Autodesk, Schwab | unknown | | identify ATS from the apply-link domain |

## Build order
1. Schema/migrations and the adapter interface.
2. Greenhouse adapter, tested against saved sample responses in `tests/fixtures/` (save real responses as fixtures; strip any personal data).
3. Workday adapter, same approach.
4. Poll loop: dedupe, 14-day window, hash-based writes, loud error handling, daily expiry.
5. Run on the verified companies and inspect the data.

## Unverified / TODO
- Whether IMC resolves on both the US and EU Greenhouse API hosts.
- Closed/removed job behavior on the Greenhouse detail endpoint.
- Rate limits of both APIs (test with a small burst; log 429s).
- Optiver's real board token; HubSpot engineering roles.
- ATS for the unknown companies above; terms-of-service review.

## Open decisions (not yet made; ask the owner before building the related feature)
| # | Decision | Notes / current lean |
|---|---|---|
| D1 | Product name | "JobSeeker" is a placeholder: generic and probably crowded in search. Shortlist: Firstlook, Sourced, Opening. Check domain, app stores, trademark first. |
| D2 | Profiles | One rule set per user, or several? Owner said "idk". Not needed until subscriptions exist; subscriptions get their own id so either works. |
| D3 | Email provider and sender address | Resend, SendGrid or plain SMTP. Needed only when alerts are built. |
| D4 | Match criteria | Keywords; title only vs title + description; location and experience-level filters. |
| D5 | Hosting and scheduling | Own machine for now, or a small VPS with cron. GitHub Actions cron is unreliable for strict 30-minute runs. |
| D6 | Workday category filtering | Per-subscription option (match facet by name per company), never a default. |
| D7 | Closed-job tracking | MVP relies on the 30-day expiry. A daily detail check or occasional full scan could be added if stale "open" jobs matter. |
| D8 | First-run behavior once alerts exist | Owner decided: silent (save only, no emails) per company on its first run. |
| D9 | Legal and terms of service | Review terms of each ATS and employer, and get legal advice before charging for the product. |
| D10 | Monetization and audience | Undecided. Test a narrow niche first (e.g. CS new grads) before broadening. |
| D11 | Which industries after CS | Teachers and healthcare are the stated targets. Their ATS landscape (school-district and public-sector platforms, hospital systems) is untested. |
| D12 | SQLite vs Postgres | Decided: Postgres. |

Settled decisions (do not reopen without asking): Python; Postgres; MVP only populates the DB; 14-day window; 30-minute recheck; 30-day soft expiry; silent first run; key jobs by `(company_id, source_job_id)`; keep `raw_payload`.

## Roadmap (do not build yet; in rough order)
1. **Alerts:** subscriptions (own id; several rule sets per user possible), keyword/location rules, notifications table (job, subscription, sent time) so nothing is emailed twice, per-company silent baseline on first run, one digest email per run grouped by `role_key`, alert the owner on zero jobs or errors.
2. **Enrichment:** flexible key/value attributes per job (YOE, salary, level, tech stack, certifications), not new columns, derived from `description_text`/`raw_payload`. Start with a regex pass; add an AI pass for fuzzy criteria only if needed. Recompute from stored data when logic improves.
3. **More ATS adapters:** confirm each company's ATS from the apply-link domain. Candidates (unverified): Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR (public feeds likely); Oracle Taleo/Recruiting Cloud, SAP SuccessFactors, iCIMS, Jobvite (harder); NEOGOV and Frontline/AppliTrack for public-sector and education.
4. **Search API and UI:** read-only service, separate process, same DB. Postgres full-text search plus ordinary indexes (company, posted_at, status, location); cache common queries. Move to Meilisearch/Typesense only past a few million jobs.
5. **Accounts and saved searches:** multiple users and profiles; match only new jobs against rules, not the whole table.
6. **Adaptive polling:** slow down large or quiet boards; test conditional requests (ETag/Last-Modified) on Greenhouse; spread requests; back off on 429. Single-IP Greenhouse traffic is the likely first limit (~1 req/s at 2,000 companies).
7. **Learning layer:** declarative "recipes" (JSON) run by a fixed engine, never stored generated code. Verify a recipe against real data before saving it, monitor for stale or empty output, keep ATS detection separate from reading. Layering: known adapters, auto-onboarding, AI-built recipes, browser scraping last.
8. **Other professions:** expand beyond CS once one niche works.
9. **Product and business:** choose a name, validate with real users, decide pricing and audience.

## Capacity notes (estimates, not measurements)
- A job is about 3 KB without content and 10-15 KB with description plus raw payload. 100,000 live jobs is about 1.5 GB; expired rows purged of heavy fields are about 1 KB.
- 2,000 companies at ~50 jobs each is about 100,000 jobs: ~96,000 list calls/day (~1.1/s), ~3,300 new-job detail calls/day, ~47,000 one-time backfill details. One small server and one Postgres instance are enough.
- Greenhouse returns the whole board each poll (roughly 14 GB/day at that scale, inbound). Workday page 1 is about 6 KB. Beyond ~20,000 companies, the polling load becomes the constraint, not storage.
- Avoid write amplification: write only on new or changed jobs.

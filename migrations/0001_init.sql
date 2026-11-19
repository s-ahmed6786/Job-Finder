-- 0001_init: companies and jobs (MVP).
-- Profession-neutral by design: nothing here is specific to CS roles.

CREATE TABLE companies (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    -- New ATS adapters extend this list in a later migration.
    ats         text        NOT NULL CHECK (ats IN ('workday', 'greenhouse')),
    -- ATS-specific settings, e.g. {"tenant": "cboe", "wd": 1, "site": "External_Career_CBOE"}
    -- or {"board_token": "databricks"}. Validated by the adapter, not the DB.
    config      jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id        bigint      NOT NULL REFERENCES companies (id) ON DELETE RESTRICT,
    -- Stable id from the ATS: Workday externalPath, Greenhouse job id.
    source_job_id     text        NOT NULL,
    -- Groups reposts / per-office duplicates (Greenhouse internal_job_id, else id).
    role_key          text,
    -- Display only. Never a key: null, repeated, or junk like "See Opening ID".
    req_id            text,
    title             text        NOT NULL,
    locations         text,                                  -- raw string as returned
    locations_list    text[]      NOT NULL DEFAULT '{}',     -- split + trimmed
    url               text        NOT NULL,                  -- taken from the response, never constructed
    posted_at         timestamptz,                           -- UTC; null if the source gives none
    description_text  text,                                  -- unescaped, tags stripped
    content_hash      text        NOT NULL,                  -- sha256 hex; drives "only write on change"
    raw_payload       jsonb       NOT NULL,                  -- personal data removed before insert
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),    -- updated only on insert or content change
    status            text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'expired')),

    CONSTRAINT jobs_company_source_uq UNIQUE (company_id, source_job_id),
    CONSTRAINT jobs_content_hash_sha256 CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

-- Daily expiry: open jobs whose effective start is older than 30 days.
CREATE INDEX jobs_open_expiry_idx
    ON jobs ((coalesce(posted_at, first_seen_at)))
    WHERE status = 'open';

-- Grouping reposts within a company (later: digests grouped by role_key).
CREATE INDEX jobs_company_role_key_idx ON jobs (company_id, role_key);

-- Common read path for the future API: recent open jobs.
CREATE INDEX jobs_status_posted_idx ON jobs (status, posted_at DESC);

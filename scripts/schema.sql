create extension if not exists vector;

create table if not exists templates (
  template_id  int primary key,
  template     text not null,
  size         bigint
);

create table if not exists tickets (
  key          text primary key,        -- HDFS-1234
  summary      text not null,
  description  text,
  status       text,
  resolution   text,
  issue_type   text,
  components   text[],
  created_at   timestamptz,
  resolved_at  timestamptz,
  split        text check (split in ('index','test')),
  raw          jsonb not null
);

create table if not exists ticket_links (
  from_key     text references tickets(key),
  to_key       text,
  link_type    text,                    -- duplicates, relates, causes
  primary key (from_key, to_key, link_type)
);

create table if not exists chunks (
  id           serial primary key,
  ticket_key   text references tickets(key) on delete cascade,
  section      text,                    -- description, comment, resolution
  content      text not null,
  template_ids int[],
  exception_signatures text[] default '{}',
  embedding    vector(768),
  created_at   timestamptz,             -- when the text was written (comment time); retrieval filters on it
  tsv          tsvector generated always as (to_tsvector('english', content)) stored
);

create table if not exists diagnoses (
  id           serial primary key,
  ticket_key   text,
  diagnosis    text,
  cited_keys   text[],
  abstained    boolean,
  created_at   timestamptz default now()
);
create index if not exists chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_ticket on chunks (ticket_key);

-- Execution: one row per job run. Bull-style queue on Postgres (FOR UPDATE SKIP LOCKED).
create table if not exists jobs (
  id            bigserial primary key,
  type          text not null,
  payload       jsonb not null default '{}',
  dedupe_key    text,
  status        text not null default 'queued'
                check (status in ('queued', 'running', 'done', 'failed')),
  attempts      int not null default 0,
  max_attempts  int not null default 3,
  run_after     timestamptz not null default now(),
  parent_id     bigint references jobs(id),
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  heartbeat_at  timestamptz,
  result        jsonb,
  error         text
);
-- at most one ACTIVE job per dedupe_key: fifty resolved tickets cause one BM25 refresh
create unique index if not exists jobs_dedupe on jobs (dedupe_key) where status in ('queued', 'running');
create index if not exists jobs_ready on jobs (run_after) where status = 'queued';
create index if not exists jobs_parent on jobs (parent_id);

-- What each step of a job did, for the dashboard. Text is always the redacted version.
create table if not exists job_events (
  id          bigserial primary key,
  job_id      bigint not null references jobs(id) on delete cascade,
  seq         int not null,
  step        text not null,
  status      text not null,
  ms          int,
  tokens      int,
  input       jsonb,
  output      jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists job_events_job on job_events (job_id, seq);

-- Outcome: one row per Jira issue (also the once-per-issue webhook check). jobs handles execution.
create table if not exists triages (
  issue_key   text primary key,
  job_id      bigint references jobs(id) on delete set null,
  summary     text not null,                  -- stored redacted
  description text not null default '',       -- stored redacted
  created_at  timestamptz not null,           -- when the issue was filed in Jira
  received_at timestamptz not null default now(),
  finished_at timestamptz,
  comment     text,                           -- what was (or would be) posted
  result      jsonb                           -- diagnosis, cost
);

-- Shared index version: the BM25 index lives in each triage worker's memory, so a refresh
-- bumps this number and workers rebuild when theirs is behind.
create table if not exists index_state (
  id            int primary key default 1 check (id = 1),
  bm25_version  int not null default 0
);
insert into index_state (id) values (1) on conflict do nothing;

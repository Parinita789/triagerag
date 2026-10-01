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
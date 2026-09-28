create extension if not exists vector;

create table if not exists blocks (
  block_id    text primary key,
  label       text not null check (label in ('Normal','Anomaly')),
  in_sample   boolean not null default false,
  line_count  int
);

create table if not exists log_lines (
  id          bigserial primary key,
  block_id    text not null references blocks(block_id),
  ts          timestamp not null,
  pid         int,
  level       text,
  component   text,
  message     text not null,
  line_number bigint not null
);

create index if not exists log_lines_block_idx on log_lines (block_id);
create index if not exists log_lines_ts_idx on log_lines (ts);
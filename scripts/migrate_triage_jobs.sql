-- One-time: move finished triages out of the old single-purpose queue table.
-- Run after schema.sql. Safe to re-run (does nothing once triage_jobs is gone).
do $$
begin
  if to_regclass('public.triage_jobs') is not null then
    insert into triages (issue_key, summary, description, created_at, received_at, finished_at, comment, result)
    select issue_key, summary, description, created_at, received_at, finished_at, comment, result
    from triage_jobs where status = 'done'
    on conflict (issue_key) do nothing;
    drop table triage_jobs;
  end if;
end $$;

import { useCallback, useEffect, useState } from "react";
import { Api, ApiError, Job, Metrics, Status, age, until } from "./api";
import { MiniLifecycle } from "./Lifecycle";

const COLUMNS: Status[] = ["queued", "running", "done", "failed"];
const POLL_MS = 2000;

export default function Board({ api }: { api: Api }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [metrics, setMetrics] = useState<Metrics>({});
  const [type, setType] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [j, m] = await Promise.all([api.jobs({ limit: 200, ...(type ? { type } : {}) }), api.metrics()]);
      setJobs(j);
      setMetrics(m);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? "Wrong API key: lock and re-enter it." : String(e));
    }
  }, [api, type]);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const totals = (s: Status) =>
    Object.entries(metrics)
      .filter(([t]) => !type || t === type)
      .reduce((n, [, by]) => n + (by[s] ?? 0), 0);

  return (
    <section>
      <div className="toolbar">
        <h2>Queue</h2>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">All types</option>
          {Object.keys(metrics).sort().map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="board">
        {COLUMNS.map((s) => (
          <div key={s} className="column">
            <h3>
              <span className={`dot ${s}`} /> {s} <span className="count">{totals(s)}</span>
            </h3>
            {jobs.filter((j) => j.status === s).slice(0, 50).map((j) => (
              <JobCard key={j.id} job={j} api={api} onChange={load} />
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

function JobCard({ job, api, onChange }: { job: Job; api: Api; onChange: () => void }) {
  const label = (job.payload.key as string) || (job.payload.summary as string) || "";
  return (
    <a className="card" href={`#/jobs/${job.id}`}>
      <div className="row">
        <strong>{job.type}</strong>
        <span className="muted">#{job.id}</span>
      </div>
      {label && <div className="label">{label}</div>}
      <div className="row muted small">
        <span>attempt {job.attempts}/{job.max_attempts}</span>
        <span>{age(job.created_at)} ago</span>
      </div>
      {job.stages && job.status !== "queued" && <MiniLifecycle stages={job.stages} steps={job.steps ?? {}} />}
      {job.status === "queued" && new Date(job.run_after).getTime() > Date.now() && (
        <div className="small muted">{job.attempts ? "retry" : "starts"} in {until(job.run_after)}</div>
      )}
      {!!job.children && <div className="small muted">{job.children} child jobs</div>}
      {job.error && <div className="error small clip">{job.error.split("\n")[0]}</div>}
      {job.status === "failed" && (
        <button
          className="small-btn"
          onClick={async (e) => {
            e.preventDefault();
            await api.retry(job.id).catch(() => {});
            onChange();
          }}
        >
          Retry
        </button>
      )}
    </a>
  );
}

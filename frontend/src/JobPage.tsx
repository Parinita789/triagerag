import { useCallback, useEffect, useState } from "react";
import { Api, JobDetail, StepEvent, age } from "./api";
import TriageInspector from "./TriageInspector";

export default function JobPage({ api, id }: { api: Api; id: number }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => api.job(id).then(setJob, (e) => setError(String(e))), [api, id]);
  useEffect(() => {
    setJob(null);
    setSelected(null);
    load();
  }, [load]);

  const live = job && (job.status === "queued" || job.status === "running");
  useEffect(() => {
    if (!live) return;
    return api.stream(id, {
      step: (ev) =>
        setJob((j) => (j && !j.events.some((e) => e.seq === ev.seq) ? { ...j, events: [...j.events, ev] } : j)),
      status: (s) => setJob((j) => (j ? { ...j, ...s, children: j.children, events: j.events, triage: j.triage } : j)),
      end: load,
    });
  }, [api, id, live, load]);

  // a fan-out parent finishes right away; keep its progress bar moving until the children do
  const childrenOpen = job && job.children.total > (job.children.done ?? 0) + (job.children.failed ?? 0);
  useEffect(() => {
    if (!childrenOpen) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [childrenOpen, load]);

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;
  const step = job.events.find((e) => e.seq === selected);

  return (
    <section>
      <div className="toolbar">
        <h2>
          <a href="#/" className="muted">Queue</a> / {job.type} #{job.id}
        </h2>
        <span className={`pill ${job.status}`}>{job.status}</span>
        {job.status === "failed" && (
          <button onClick={() => api.retry(job.id).then(load)}>Retry</button>
        )}
      </div>

      <dl className="facts">
        <div><dt>Attempts</dt><dd>{job.attempts}/{job.max_attempts}</dd></div>
        <div><dt>Created</dt><dd>{age(job.created_at)} ago</dd></div>
        <div><dt>Duration</dt><dd>{duration(job.started_at, job.finished_at)}</dd></div>
        {job.dedupe_key && <div><dt>Dedupe key</dt><dd>{job.dedupe_key}</dd></div>}
        {job.parent_id && <div><dt>Parent</dt><dd><a href={`#/jobs/${job.parent_id}`}>#{job.parent_id}</a></dd></div>}
      </dl>

      {job.error && (
        <details className="error-box" open={job.status === "failed"}>
          <summary>Last error (attempt {job.attempts})</summary>
          <pre>{job.error}</pre>
        </details>
      )}

      {job.children.total > 0 && <Progress children={job.children} />}

      <h3>Steps {live && <span className="live">live</span>}</h3>
      {job.events.length === 0 ? (
        <p className="muted">{live ? "Waiting for the first step…" : "This job recorded no steps."}</p>
      ) : (
        <Waterfall events={job.events} selected={selected} onSelect={setSelected} />
      )}
      {step && <StepDetail step={step} />}

      {job.type === "triage_ticket" && job.events.length > 0 && <TriageInspector job={job} />}

      {job.result && (
        <details>
          <summary>Result</summary>
          <pre>{JSON.stringify(job.result, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}

function duration(start: string | null, end: string | null): string {
  if (!start) return "–";
  const ms = (end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime();
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function Progress({ children }: { children: JobDetail["children"] }) {
  const done = children.done ?? 0;
  const failed = children.failed ?? 0;
  const pct = (n: number) => `${(100 * n) / children.total}%`;
  return (
    <div className="progress-wrap">
      <div className="row small">
        <span>Child jobs</span>
        <span>
          {done + failed}/{children.total} finished{failed ? `, ${failed} failed` : ""}
        </span>
      </div>
      <div className="progress">
        <div className="bar done" style={{ width: pct(done) }} />
        <div className="bar failed" style={{ width: pct(failed) }} />
      </div>
    </div>
  );
}

function Waterfall({ events, selected, onSelect }: { events: StepEvent[]; selected: number | null; onSelect: (s: number) => void }) {
  const spans = events.map((e) => {
    const end = new Date(e.created_at).getTime();
    return { e, start: end - (e.ms ?? 0), end };
  });
  const t0 = Math.min(...spans.map((s) => s.start));
  const total = Math.max(1, Math.max(...spans.map((s) => s.end)) - t0);
  return (
    <div className="waterfall">
      {spans.map(({ e, start, end }) => (
        <button key={e.seq} className={`wf-row ${selected === e.seq ? "selected" : ""}`} onClick={() => onSelect(e.seq)}>
          <span className="wf-name">
            <span className={`dot ${e.status === "error" ? "failed" : "done"}`} /> {e.step}
          </span>
          <span className="wf-track">
            <span
              className={`wf-bar ${e.status}`}
              style={{ left: `${(100 * (start - t0)) / total}%`, width: `max(3px, ${(100 * (end - start)) / total}%)` }}
            />
          </span>
          <span className="wf-ms">
            {e.ms != null ? `${e.ms} ms` : ""}
            {e.tokens ? ` · ${e.tokens} tok` : ""}
          </span>
        </button>
      ))}
    </div>
  );
}

function StepDetail({ step }: { step: StepEvent }) {
  return (
    <div className="step-detail">
      <h4>
        #{step.seq} {step.step} <span className={`pill ${step.status === "error" ? "failed" : "done"}`}>{step.status}</span>
      </h4>
      <div className="two-col">
        <div>
          <h5>Input</h5>
          <pre>{step.input == null ? "–" : JSON.stringify(step.input, null, 2)}</pre>
        </div>
        <div>
          <h5>Output</h5>
          <pre>{step.output == null ? "–" : JSON.stringify(step.output, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}

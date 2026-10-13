import { useCallback, useEffect, useMemo, useState } from "react";
import { Api, JobDetail, StepEvent, age } from "./api";
import Lifecycle, { fmtMs, lifecycle } from "./Lifecycle";
import TriageInspector from "./TriageInspector";

export default function JobPage({ api, id }: { api: Api; id: number }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [attempt, setAttempt] = useState<number | null>(null); // null: follow the current attempt
  const [picked, setPicked] = useState<string | null>(null); // null: follow the running step
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => api.job(id).then(setJob, (e) => setError(String(e))), [api, id]);
  useEffect(() => {
    setJob(null);
    setPicked(null);
    setAttempt(null);
    load();
  }, [load]);

  const live = job && (job.status === "queued" || job.status === "running");
  useEffect(() => {
    if (!live) return;
    return api.stream(id, {
      // a step arrives when it starts and again when it ends: replace it by seq
      step: (ev) =>
        setJob((j) => {
          if (!j) return j;
          const events = j.events.filter((e) => e.seq !== ev.seq);
          events.push(ev);
          events.sort((a, b) => a.seq - b.seq);
          return { ...j, events };
        }),
      status: (s) => setJob((j) => (j ? { ...j, ...s, stages: j.stages, children: j.children, events: j.events, triage: j.triage } : j)),
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

  const shownAttempt = attempt ?? Math.max(job?.attempts ?? 1, 1);
  const nodes = useMemo(() => (job ? lifecycle(job, shownAttempt) : []), [job, shownAttempt]);

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;

  const running = nodes.find((n) => n.status === "running" && n.event);
  const lastDone = [...nodes].reverse().find((n) => n.event);
  const selectedKey = picked ?? running?.key ?? (job.status === "failed" ? "end" : lastDone?.key ?? null);
  const selected = nodes.find((n) => n.key === selectedKey);
  const extra = job.events.filter((e) => e.attempt === shownAttempt && !job.stages.includes(e.step));

  return (
    <section>
      <div className="toolbar">
        <h2>
          <a href="#/" className="muted">Queue</a> / {job.type} #{job.id}
        </h2>
        <span className={`pill ${job.status}`}>{job.status}</span>
        {live && <span className="live">live</span>}
        {job.status === "failed" && <button onClick={() => api.retry(job.id).then(load)}>Retry</button>}
      </div>

      <dl className="facts">
        <div><dt>Created</dt><dd>{age(job.created_at)} ago</dd></div>
        <div><dt>Duration</dt><dd>{duration(job.started_at, job.finished_at)}</dd></div>
        <div><dt>Attempts</dt><dd>{job.attempts}/{job.max_attempts}</dd></div>
        {(job.payload.key as string) && <div><dt>Ticket</dt><dd>{job.payload.key as string}</dd></div>}
        {job.parent_id && <div><dt>Parent</dt><dd><a href={`#/jobs/${job.parent_id}`}>#{job.parent_id}</a></dd></div>}
      </dl>

      {job.attempts > 1 && (
        <div className="tabs">
          {Array.from({ length: job.attempts }, (_, i) => i + 1).map((a) => (
            <button key={a} className={a === shownAttempt ? "tab active" : "tab"} onClick={() => (setAttempt(a), setPicked(null))}>
              Attempt {a}
            </button>
          ))}
        </div>
      )}

      <h3>Lifecycle</h3>
      <Lifecycle nodes={nodes} selected={selectedKey} onSelect={setPicked} />

      {selected?.event && <StepDetail step={selected.event} info={selected.info} />}
      {selected?.key === "end" && <EndDetail job={job} current={shownAttempt === Math.max(job.attempts, 1)} />}

      {job.children.total > 0 && <Progress children={job.children} />}

      {extra.length > 0 && (
        <details className="extra">
          <summary>Other events in this attempt ({extra.length}): tool calls, index sync</summary>
          {extra.map((e) => (
            <details key={e.seq} className="extra-row">
              <summary>
                #{e.seq} <code>{e.step}</code> <span className="muted small">{e.status} {fmtMs(e.ms)}</span>
              </summary>
              <IO step={e} />
            </details>
          ))}
        </details>
      )}

      {job.type === "triage_ticket" && job.events.length > 0 && <TriageInspector job={job} attempt={shownAttempt} />}
    </section>
  );
}

function duration(start: string | null, end: string | null): string {
  if (!start) return "–";
  return fmtMs((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime());
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

function StepDetail({ step, info }: { step: StepEvent; info?: string }) {
  const what = (step.output as { what?: string } | null)?.what;
  return (
    <div className="step-detail">
      <h4>
        {step.step} <span className={`pill ${step.status === "error" ? "failed" : step.status === "running" ? "running" : "done"}`}>{step.status}</span>
        <span className="muted small"> {fmtMs(step.ms)}{step.tokens ? ` · ${step.tokens} tokens` : ""}</span>
      </h4>
      {(what || info) && <p className="muted">{what ?? info}</p>}
      <IO step={step} />
    </div>
  );
}

function IO({ step }: { step: StepEvent }) {
  const strip = (v: unknown) => {
    if (v && typeof v === "object" && !Array.isArray(v) && "what" in v) {
      const { what: _, ...rest } = v as Record<string, unknown>;
      return rest;
    }
    return v;
  };
  return (
    <div className="two-col">
      <div>
        <h5>Input</h5>
        <pre>{step.input == null ? "–" : JSON.stringify(step.input, null, 2)}</pre>
      </div>
      <div>
        <h5>Output</h5>
        <pre>{step.status === "running" && step.output == null ? "running…" : step.output == null ? "–" : JSON.stringify(strip(step.output), null, 2)}</pre>
      </div>
    </div>
  );
}

function EndDetail({ job, current }: { job: JobDetail; current: boolean }) {
  if (current && job.status === "done")
    return (
      <div className="step-detail">
        <h4>Result</h4>
        <pre>{JSON.stringify(job.result, null, 2)}</pre>
      </div>
    );
  return (
    <div className="step-detail">
      <h4 className="error">{current ? "Error" : "This attempt failed"}</h4>
      <pre>{job.error ?? "The error of an earlier attempt is replaced by the latest one."}</pre>
    </div>
  );
}

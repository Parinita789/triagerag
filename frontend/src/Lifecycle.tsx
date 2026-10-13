import { JobDetail, StepEvent, StepStatus } from "./api";
import { STAGE_INFO } from "./stages";

type NodeStatus = StepStatus | "pending";

export interface LifecycleNode {
  key: string;
  label: string;
  info?: string;
  status: NodeStatus;
  ms?: number | null;
  event?: StepEvent;
  runs?: number;
}

const ICON: Record<NodeStatus, string> = { pending: "", running: "", ok: "✓", error: "✕", skipped: "–" };

/** The job's lifecycle for one attempt: queued → claimed → each declared stage → done / failed. */
export function lifecycle(job: JobDetail, attempt: number): LifecycleNode[] {
  const events = job.events.filter((e) => e.attempt === attempt);
  const latest = (step: string) => [...events].reverse().find((e) => e.step === step);
  const isCurrent = attempt === Math.max(job.attempts, 1);
  const claimed = job.attempts >= attempt;
  const wait = isCurrent && job.started_at ? new Date(job.started_at).getTime() - new Date(job.created_at).getTime() : null;
  const nodes: LifecycleNode[] = [
    { key: "queued", label: "queued", info: job.dedupe_key ? `dedupe key: ${job.dedupe_key}` : "waiting for a worker", status: "ok" },
    {
      key: "claimed",
      label: `attempt ${attempt}/${job.max_attempts}`,
      info: claimed ? "claimed by a worker (FOR UPDATE SKIP LOCKED)" : "waiting to be claimed",
      status: claimed ? "ok" : "running",
      ms: attempt === 1 && wait != null && wait >= 0 ? wait : null,
    },
  ];
  for (const stage of job.stages) {
    const e = latest(stage);
    nodes.push({
      key: stage,
      label: stage,
      info: STAGE_INFO[stage],
      status: e ? e.status : "pending",
      ms: e?.ms,
      event: e,
      runs: events.filter((x) => x.step === stage).length,
    });
  }
  nodes.push(endNode(job, isCurrent));
  return nodes;
}

function endNode(job: JobDetail, isCurrent: boolean): LifecycleNode {
  const base = { key: "end" };
  if (!isCurrent) return { ...base, label: "failed → retried", info: "this attempt failed", status: "error" };
  if (job.status === "done") return { ...base, label: "done", info: "result stored", status: "ok" };
  if (job.status === "failed") return { ...base, label: "failed", info: "no attempts left: retry it by hand", status: "error" };
  if (job.status === "queued" && job.attempts > 0)
    return { ...base, label: "retry scheduled", info: `backoff until ${new Date(job.run_after).toLocaleTimeString()}`, status: "error" };
  return { ...base, label: "done", status: "pending" };
}

export default function Lifecycle({
  nodes,
  selected,
  onSelect,
}: {
  nodes: LifecycleNode[];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  return (
    <ol className="lifecycle">
      {nodes.map((n, i) => (
        <li key={n.key} className={`lc-node ${n.status} ${selected === n.key ? "selected" : ""}`}>
          {i > 0 && <span className={`lc-edge ${n.status === "pending" ? "" : "reached"}`} />}
          <button onClick={() => onSelect(n.key)} disabled={!n.event && n.key !== "end"} title={n.info}>
            <span className="lc-dot">{ICON[n.status]}</span>
            <span className="lc-label">
              {n.label}
              {n.runs && n.runs > 1 ? <span className="lc-runs">×{n.runs}</span> : null}
            </span>
            <span className="lc-ms">{n.status === "running" ? "running…" : fmtMs(n.ms)}</span>
            {n.info && <span className="lc-info">{n.info}</span>}
          </button>
        </li>
      ))}
    </ol>
  );
}

/** Compact version for the queue board cards: one segment per stage. */
export function MiniLifecycle({ stages, steps }: { stages: string[]; steps: Record<string, StepStatus> }) {
  if (!stages.length) return null;
  const current = stages.find((s) => steps[s] === "running");
  const done = stages.filter((s) => steps[s] === "ok" || steps[s] === "skipped").length;
  return (
    <div className="mini">
      <div className="mini-bar">
        {stages.map((s) => (
          <span key={s} className={`mini-seg ${steps[s] ?? "pending"}`} title={`${s}: ${steps[s] ?? "pending"}`} />
        ))}
      </div>
      <div className="small muted">
        {current ? <>▶ <strong>{current}</strong> · </> : null}
        {done}/{stages.length} steps
      </div>
    </div>
  );
}

export function fmtMs(ms?: number | null): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60_000)} min`;
}

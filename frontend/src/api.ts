export type Status = "queued" | "running" | "done" | "failed";

export interface StepEvent {
  seq: number;
  step: string;
  status: "ok" | "error";
  ms: number | null;
  tokens: number | null;
  input: unknown;
  output: unknown;
  created_at: string;
}

export interface Job {
  id: number;
  type: string;
  status: Status;
  attempts: number;
  max_attempts: number;
  dedupe_key: string | null;
  parent_id: number | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  run_after: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  children?: number;
}

export interface Triage {
  issue_key: string;
  summary: string;
  description: string;
  comment: string | null;
  result: Record<string, unknown> | null;
}

export interface JobDetail extends Omit<Job, "children"> {
  events: StepEvent[];
  children: { total: number } & Partial<Record<Status, number>>;
  triage: Triage | null;
}

export type Metrics = Record<string, Partial<Record<Status, number>>>;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Thin fetch wrapper. The API key lives only in memory (React state), never in storage. */
export class Api {
  constructor(private key: string) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const r = await fetch(path, {
      method,
      headers: { "X-API-Key": this.key, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      throw new ApiError(r.status, detail.detail ?? r.statusText);
    }
    return r.json();
  }

  jobs(params: Record<string, string | number> = {}) {
    const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
    return this.request<Job[]>("GET", `/jobs?${q}`);
  }
  job(id: number) {
    return this.request<JobDetail>("GET", `/jobs/${id}`);
  }
  metrics() {
    return this.request<Metrics>("GET", "/metrics/jobs");
  }
  enqueue(type: string, payload: Record<string, unknown>) {
    return this.request<{ id: number }>("POST", "/jobs", { type, payload });
  }
  retry(id: number) {
    return this.request<{ id: number }>("POST", `/jobs/${id}/retry`);
  }

  /** Live steps over SSE. EventSource can't send headers, so the key goes in the query string. */
  stream(id: number, on: { step: (e: StepEvent) => void; status: (j: Job) => void; end: () => void }) {
    const es = new EventSource(`/jobs/${id}/stream?key=${encodeURIComponent(this.key)}`);
    es.addEventListener("step", (m) => on.step(JSON.parse((m as MessageEvent).data)));
    es.addEventListener("status", (m) => on.status(JSON.parse((m as MessageEvent).data)));
    es.addEventListener("end", () => {
      es.close();
      on.end();
    });
    es.onerror = () => es.close();
    return () => es.close();
  }
}

export function age(iso: string | null): string {
  if (!iso) return "–";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

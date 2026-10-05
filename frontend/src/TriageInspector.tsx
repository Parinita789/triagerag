import { Fragment } from "react";
import { JobDetail, StepEvent } from "./api";

type Ranked = { key: string; rank: number; score?: number; dense_rank?: number | null; bm25_rank?: number | null };
type Llm = { reply: string; tool_calls: { name: string; arguments: string }[]; finish_reason: string; cache_hit: boolean | null };

const PSEUDONYM = /(<[A-Z_]+_[0-9a-f]{8}>)/;  // not /g: .test() on a /g regex keeps state

/** Redaction pseudonyms like <AWS_KEY_893ed64f> highlighted. The text is always the redacted version. */
export function Redacted({ text }: { text: string }) {
  return (
    <>
      {text.split(PSEUDONYM).map((part, i) =>
        PSEUDONYM.test(part) ? <mark key={i} title="redacted">{part}</mark> : <Fragment key={i}>{part}</Fragment>,
      )}
    </>
  );
}

const first = (events: StepEvent[], step: string) => events.find((e) => e.step === step);

export default function TriageInspector({ job }: { job: JobDetail }) {
  const ev = job.events;
  const redact = first(ev, "redact")?.output as { summary: string; description: string; counts: Record<string, number> } | undefined;
  const dense = (first(ev, "retrieve.dense")?.output ?? []) as Ranked[];
  const bm25 = (first(ev, "retrieve.bm25")?.output ?? []) as Ranked[];
  const fused = (first(ev, "rrf")?.output ?? []) as Ranked[];
  const llmEvents = ev.filter((e) => e.step === "llm");
  const validate = first(ev, "validate")?.output as { kept: string[]; dropped: string[]; errors: string[] } | undefined;
  const comment = (first(ev, "comment")?.output as { comment: string } | undefined)?.comment ?? job.triage?.comment;

  const rows = Math.max(dense.length, bm25.length, fused.length);
  const fusedKeys = new Set(fused.map((f) => f.key));

  return (
    <div className="inspector">
      <h3>Triage inspector</h3>

      {redact && (
        <div className="panel">
          <h4>
            Ticket text{" "}
            <span className="muted small">
              {Object.keys(redact.counts).length
                ? "redacted: " + Object.entries(redact.counts).map(([k, n]) => `${k} ×${n}`).join(", ")
                : "nothing redacted"}
            </span>
          </h4>
          <p><strong><Redacted text={redact.summary} /></strong></p>
          <pre className="prose"><Redacted text={redact.description} /></pre>
        </div>
      )}

      {rows > 0 && (
        <div className="panel">
          <h4>Retrieval: dense, BM25 and fused (RRF) side by side</h4>
          <table className="ranks">
            <thead>
              <tr><th>#</th><th>Dense</th><th>BM25</th><th>RRF (dense rank / BM25 rank)</th></tr>
            </thead>
            <tbody>
              {Array.from({ length: rows }, (_, i) => (
                <tr key={i}>
                  <td className="muted">{i + 1}</td>
                  <RankCell hit={dense[i]} hl={fusedKeys} />
                  <RankCell hit={bm25[i]} hl={fusedKeys} />
                  <td>
                    {fused[i] && (
                      <>
                        <strong>{fused[i].key}</strong>{" "}
                        <span className="muted small">{fused[i].dense_rank ?? "–"} / {fused[i].bm25_rank ?? "–"}</span>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {llmEvents.map((e) => {
        const out = e.output as Llm | { error: string };
        const sent = (e.input as { messages: { role: string; content: string }[] })?.messages ?? [];
        return (
          <div className="panel" key={e.seq}>
            <h4>
              LLM call <span className="muted small">{e.ms} ms · {e.tokens ?? 0} tokens{"cache_hit" in out && out.cache_hit ? " · cached" : ""}</span>
            </h4>
            <details>
              <summary>Prompt ({sent.length} messages)</summary>
              {sent.map((m, i) => (
                <div key={i} className="msg">
                  <span className="role">{m.role}</span>
                  <pre className="prose"><Redacted text={m.content} /></pre>
                </div>
              ))}
            </details>
            {"error" in out ? (
              <p className="error">{out.error}</p>
            ) : (
              <>
                {out.reply && <pre className="prose">{out.reply}</pre>}
                {out.tool_calls.map((tc, i) => (
                  <div key={i} className="msg">
                    <span className="role">{tc.name}</span>
                    <pre>{pretty(tc.arguments)}</pre>
                  </div>
                ))}
                <p className="muted small">finish reason: {out.finish_reason}</p>
              </>
            )}
          </div>
        );
      })}

      {validate && (
        <div className="panel">
          <h4>Citations</h4>
          <p>
            Kept: {validate.kept.length ? validate.kept.map((k) => <span key={k} className="chip ok">{k}</span>) : <span className="muted">none</span>}
          </p>
          <p>
            Dropped (never seen in tool results):{" "}
            {validate.dropped.length ? validate.dropped.map((k) => <span key={k} className="chip bad">{k}</span>) : <span className="muted">none</span>}
          </p>
          {validate.errors.map((e, i) => <p key={i} className="error small">{e}</p>)}
        </div>
      )}

      {comment && (
        <div className="panel">
          <h4>Comment {job.triage && <span className="muted small">for {job.triage.issue_key} (dry run, not posted)</span>}</h4>
          <pre className="prose comment"><Redacted text={comment} /></pre>
        </div>
      )}
    </div>
  );
}

function RankCell({ hit, hl }: { hit?: Ranked; hl: Set<string> }) {
  if (!hit) return <td />;
  return (
    <td className={hl.has(hit.key) ? "in-fused" : ""}>
      {hit.key} <span className="muted small">{hit.score?.toFixed(3)}</span>
    </td>
  );
}

function pretty(args: string): string {
  try {
    return JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    return args;
  }
}

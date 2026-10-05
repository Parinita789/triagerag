import { useState } from "react";
import { Api } from "./api";

const EXAMPLE = {
  summary: "DataNode block report rejected after restart",
  description:
    "After restarting dn3 the NameNode rejects its full block report and marks its blocks as missing. " +
    "Storage IDs in the log differ from before the restart. Reported by ops@corp.com, aws key AKIA1234567890ABCDEF.",
};

export default function TryIt({ api }: { api: Api }) {
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <section className="narrow">
      <h2>Try it</h2>
      <p className="muted">
        Paste a ticket. It's redacted when it's enqueued, then a triage worker runs retrieval and the LLM; every step
        streams in live on the next page. Uses one LLM call.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const { id } = await api.enqueue("triage_ticket", { summary, description });
            location.hash = `#/jobs/${id}`;
          } catch (err) {
            setError(String(err));
            setBusy(false);
          }
        }}
      >
        <label>
          Summary
          <input required value={summary} onChange={(e) => setSummary(e.target.value)} />
        </label>
        <label>
          Description
          <textarea rows={10} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="row">
          <button type="button" className="ghost" onClick={() => (setSummary(EXAMPLE.summary), setDescription(EXAMPLE.description))}>
            Fill example
          </button>
          <button type="submit" disabled={busy || !summary.trim()}>
            {busy ? "Submitting…" : "Triage it"}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </form>
    </section>
  );
}

import { useEffect, useState } from "react";
import { Api } from "./api";
import Board from "./Board";
import JobPage from "./JobPage";
import TryIt from "./TryIt";

function useHash(): string {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(location.hash || "#/");
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function KeyGate({ onKey }: { onKey: (k: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="gate"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) onKey(value.trim());
      }}
    >
      <h1>triagerag</h1>
      <p className="muted">Enter the API key. It stays in memory for this tab only.</p>
      <input type="password" autoFocus placeholder="X-API-Key" value={value} onChange={(e) => setValue(e.target.value)} />
      <button type="submit">Open dashboard</button>
    </form>
  );
}

export default function App() {
  const [api, setApi] = useState<Api | null>(null);
  const hash = useHash();
  if (!api) return <KeyGate onKey={(k) => setApi(new Api(k))} />;

  const job = hash.match(/^#\/jobs\/(\d+)/);
  const page = job ? <JobPage api={api} id={Number(job[1])} /> : hash.startsWith("#/try") ? <TryIt api={api} /> : <Board api={api} />;
  return (
    <>
      <header>
        <a className="brand" href="#/">triagerag</a>
        <nav>
          <a href="#/" className={hash === "#/" ? "active" : ""}>Queue</a>
          <a href="#/try" className={hash.startsWith("#/try") ? "active" : ""}>Try it</a>
        </nav>
        <button className="ghost" onClick={() => setApi(null)}>Lock</button>
      </header>
      <main>{page}</main>
    </>
  );
}

/** What each lifecycle step does, shown under its name in the dashboard. */
export const STAGE_INFO: Record<string, string> = {
  // triage
  clean: "Strip Jira markup, split code blocks from prose",
  redact: "Replace secrets and PII with pseudonyms",
  extract: "Find log lines and exceptions, match Drain templates",
  embed: "Turn text into vectors (bge-base, 768 dims)",
  dense: "Nearest chunks by cosine similarity (pgvector)",
  bm25: "Keyword search: rare words weigh more",
  hybrid: "Fuse dense + BM25 ranks (reciprocal rank fusion)",
  rerank: "Cross-encoder reordering: shown, not used",
  llm: "Diagnosis from the retrieved tickets",
  validate: "Drop citations the LLM was never shown",
  comment: "The Jira comment (dry run)",
  // indexing
  fetch: "Download the ticket from Jira",
  load: "Read raw tickets from Postgres",
  chunk: "Split into sections of at most 350 words",
  store: "Replace the tickets' chunks in one transaction",
  refresh_bm25: "Queue a BM25 refresh for the triage workers",
  select: "Pick the tickets to index",
  fan_out: "One child job per 200 tickets",
  bump_version: "Tell triage workers to rebuild BM25",
  push: "Parse log lines and push them to Loki",
};

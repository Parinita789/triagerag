# loginsights

Log anomaly detection and explanation pipeline built on HDFS production logs.

Most "RAG on logs" systems embed raw lines and call it done. This project shows why that fails and builds something that actually works: parse logs into templates, detect anomalies from sequence behavior, retrieve correlated evidence, and generate cited explanations grounded in real signals.

---

## What it does

1. **Parse** — Drain converts 11M raw log lines into ~46 templates. 11M lines become a manageable vocabulary.
2. **Detect** — anomaly scoring from five signals: template frequency deltas, novel templates, sequence violations, error bursts, and absence of expected events.
3. **Retrieve** — two separate indexes: correlated log windows from other components, and prose knowledge (docs, past incidents) that explains what the anomaly means.
4. **Explain** — LLM assembles anomaly evidence + retrieved context into a cited hypothesis, with a first-class abstention path when evidence is insufficient.

---

## Why not just embed the logs?

Logs are near-duplicates at scale. 11M lines compress to ~46 templates — embedding the raw lines produces a million near-identical vectors and retrieval returns noise. More importantly, the signal lives in *structure*, not semantics: rate changes, ordering violations, absence of expected lines. Vector similarity is blind to all of that.

The strawman baseline (Phase 3) proves this with a number, not an argument.

---

## Dataset

**HDFS v1** from [LogHub](https://github.com/logpai/loghub) — 11,175,629 lines, 38.7 hours of real Hadoop production logs, labeled at block level by domain experts. Labels reflect execution semantics, not log level — most anomalous blocks contain no ERROR lines. That's what makes keyword grep fail and sequence modeling matter.

- 575,062 blocks total, 2.93% anomalous
- Working sample: 5,000 blocks, stratified to preserve the imbalance
- Ported to OpenStack (multi-service) and Thunderbird (1,241 templates) in Phase 8

---

## Stack

- **Python 3.12**, `uv` for dependency management
- **PostgreSQL + pgvector** — log lines, templates, embeddings, BM25 via tsvector
- **Drain3** — streaming log parser
- **sentence-transformers** (`BAAI/bge-base-en-v1.5`) — dense embeddings
- **psycopg** — async Postgres driver
- **FastAPI** — explanation API
- **pytest** — unit tests and retrieval eval harness

---

## Project structure

```
loginsight/
├── src/loginsight/
│   ├── ingest/        # streaming reader, label loader, sampler
│   ├── parse/         # Drain wrapper, template storage
│   ├── detect/        # five anomaly signals, block scorer
│   ├── retrieve/      # log index, knowledge index, RRF fusion, reranker
│   └── explain/       # LLM assembly, citation, abstention
├── scripts/           # one-off entrypoints: load, eval, benchmark
├── tests/
└── data/              # gitignored — HDFS.log, anomaly_label.csv
```

---

## Setup

```bash
# clone and install
git clone https://github.com/<you>/loginsight
cd loginsight
uv sync

# start postgres
docker compose up -d

# apply schema
docker compose exec -T db psql -U postgres -d loginsight -f - < scripts/schema.sql

# download data
mkdir data && cd data
curl -L "https://zenodo.org/records/8196385/files/HDFS_v1.zip?download=1" -o HDFS_v1.zip
unzip HDFS_v1.zip && cd ..

# ingest
uv run python scripts/load_labels.py
uv run python scripts/sample_blocks.py
uv run python scripts/load_log_lines.py
```

`.env`:
```
DATABASE_URL=postgresql://postgres:dev@localhost:5433/loginsight
DATA_DIR=./data
```

---

## Evaluation

Every phase has a number attached. Results reported on the 5,000-block sample unless noted.

| Phase | Metric | Result |
|-------|--------|--------|
| Parsing | Template accuracy vs ground truth | TBD |
| Baseline (raw embed) | F1 on anomalous blocks | TBD |
| Detection | Precision / Recall / F1 vs DeepLog baseline | TBD |
| Knowledge retrieval — dense only | Recall@5 | TBD |
| Knowledge retrieval — BM25 only | Recall@5 | TBD |
| Knowledge retrieval — RRF + rerank | Recall@5 | TBD |
| Full pipeline — HDFS | F1 | TBD |
| Full pipeline — Thunderbird (1,241 templates) | F1 | TBD |

The gap between HDFS and Thunderbird is the finding, not a limitation.

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Skeleton, Docker, schema | ✅ |
| 1 | Ingestion, sampling, line load | ✅ |
| 2 | Drain parsing, template accuracy | 🔄 |
| 3 | Strawman baseline (raw embeddings) | ⬜ |
| 4 | Detection — five signals, F1 | ⬜ |
| 5 | Log retrieval — correlated windows | ⬜ |
| 6 | Knowledge retrieval — hybrid + rerank | ⬜ |
| 7 | Explanation layer, abstention | ⬜ |
| 8 | Port to Thunderbird + OpenStack | ⬜ |

---

## Key design decisions

**Stratified sampling preserves the 2.93% anomaly rate.** Rebalancing to 50/50 deletes the actual problem — precision at low base rates is what matters in production and what the eval measures.

**Abstention is a first-class outcome.** The explainer returns "insufficient evidence" rather than a low-confidence guess. False certainty is worse than no answer.

**Two retrieval indexes, not one.** Log retrieval finds correlated activity by time window and component — mostly filters, barely embeddings. Knowledge retrieval finds prose explanations — this is where hybrid search and reranking earn their place. Conflating them into one index produces a system that's mediocre at both.

**The strawman baseline runs before any tuning.** Pure vector search over raw lines is measured and kept. That failure number is the clearest argument for everything that comes after.
# triagerag

A RAG triage pipeline for distributed-system failures. When a new Jira ticket arrives, it normalizes any logs and stack traces in the ticket, retrieves similar past incidents, and posts an LLM-generated diagnosis that cites its sources, or says it doesn't have enough evidence.

Built on real data: Apache HDFS Jira history as the knowledge base, and 11M lines of HDFS production logs for log-template vocabulary.

---

## How it works

The system has two pipelines. They share one database and one Drain template model, and nothing else.

```
INDEXING (offline, run once, then incrementally)
  HDFS logs ──→ Drain ──→ template model ─────────────────┐
                                                           ▼
  Jira API ──→ raw JSON ──→ clean ──→ extract logs ──→ normalize ──→ chunk ──→ embed ──→ Postgres
                                                                                           │
QUERY (online, per new ticket)                                                             │
  New ticket ──→ same clean/extract/normalize ──→ hybrid retrieval ◀───────────────────────┘
             ──→ rerank ──→ LLM diagnosis (cited, or abstain) ──→ post to Jira
```

The query side reuses the indexing code for cleaning and normalization. If a new ticket gets processed differently from indexed tickets, their template IDs won't match and retrieval quietly degrades.

### Part 1: Indexing

**1. Train the template model.** Drain streams all 11M HDFS log lines once and learns the templates. Masking rules replace IPs, ports, and block IDs before mining, so `10.251.43.21:50010:Transmitted block ...` and `10.251.65.237:50010:Transmitted block ...` become one template instead of hundreds. The trained model is saved to disk and reused everywhere after that. Evaluated against the 29 ground-truth templates.

**2. Fetch tickets.** Resolved HDFS tickets come from Apache's public Jira REST API, paged 100 at a time. Raw JSON is saved to `data/jira/` first, so the API is hit once and every later step re-runs from disk.

**3. Clean.** Jira wiki markup (`{code}`, `{noformat}`, `h2.`, `*bold*`) is stripped from prose. Bot comments (Hadoop QA build results, Jenkins) are dropped because they're the same boilerplate on thousands of tickets and would dominate retrieval. Code and log blocks are kept as-is for the next step.

**4. Extract and normalize logs.** Log lines and stack traces are pulled out of `{code}`/`{noformat}` blocks. Each log line goes through the same masking and the trained Drain model to get a template ID. For stack traces, the exception class plus the top few frames becomes a normalized signature. The raw text stays in the chunk. The template IDs and signatures are stored alongside it as structured fields.

**5. Chunk.** Tickets are chunked by structure, not by fixed token windows:

| Chunk | Content | Why separate |
|-------|---------|--------------|
| Problem | Summary + description | How the issue was reported; this is what a new ticket looks like |
| Comment | One per substantive comment | Diagnosis happens in comments, often several replies in |
| Resolution | Final comments + fix summary | What actually fixed it; this is what the diagnosis cites |

Any chunk over ~500 tokens is split on paragraph boundaries with overlap. Every chunk has the ticket summary prepended before embedding, so a comment that just says "the lease recovery never ran" still carries its ticket context.

**6. Attach metadata.** Each chunk stores `ticket_key`, `section`, `components`, `issue_type`, `created_at`, `resolved_at`, `template_ids[]`, and `exception_signatures[]`. Retrieval filters and the temporal split both depend on these.

**7. Embed and store.** Chunks are embedded with `bge-base-en-v1.5` and written to Postgres: the vector to a pgvector HNSW index, the text to a `tsvector` GIN index for BM25, and the template IDs to a GIN array index.

### Part 2: Query

**1. Process the new ticket** with the same clean, extract, and normalize code from indexing.

**2. Retrieve from three sources in parallel**, all restricted to tickets resolved before the query ticket was created:
- **Dense**: cosine similarity on the embedding
- **BM25**: full-text rank, strong on exact error strings and class names
- **Template match**: overlap between the query's template IDs or exception signatures and each chunk's

**3. Fuse** the three ranked lists with Reciprocal Rank Fusion.

**4. Rerank** the top 20 with a cross-encoder and keep the top 5.

**5. Diagnose.** Claude gets the ticket and the top chunks and writes a diagnosis that cites specific tickets. If the evidence is weak, it abstains.

**6. Post** the diagnosis as a Jira comment.

---

## Why normalize logs first

Two tickets describing the same failure rarely share text. Their stack traces contain different IPs, block IDs, ports, and timestamps. Embedding raw log text treats those as different, and keyword search misses them too.

Drain collapses log lines into templates. `Transmitted block blk_-1608999687919862906 to /10.251.65.203:50010` becomes `Transmitted block <BLK> to <IP>`. Once normalized, two tickets with the same failure match on template ID, regardless of which machine or block was involved. Template match is one of the retrieval signals, measured alongside dense and BM25.

---

## Data

**Apache HDFS Jira**: resolved HDFS tickets pulled from Apache's public Jira, including summary, description, comments, resolution, and issue links. This is the retrieval corpus. Issue links ("duplicates", "is related to", "is caused by") were created by Hadoop engineers and serve as retrieval ground truth.

**HDFS v1 logs** from [LogHub](https://github.com/logpai/loghub): 11,175,629 lines over 38.7 hours from a real Hadoop cluster, with 29 ground-truth templates. Used to train and evaluate the Drain parser.

**Scope note:** the logs and the tickets come from different sources and don't describe the same incidents. The logs are used for template vocabulary and parser evaluation, not to diagnose specific tickets.

---

## Evaluation

**Temporal split.** Only tickets resolved before a cutoff date are indexed. Tickets after the cutoff are test queries. Without this, retrieval can "find" tickets from the future, and the numbers mean nothing.

| Component | Metric | Result |
|-----------|--------|--------|
| Drain parsing | Template accuracy vs 29 ground-truth templates | TBD |
| Retrieval: dense only | Recall@5 / MRR | TBD |
| Retrieval: BM25 only | Recall@5 / MRR | TBD |
| Retrieval: template match only | Recall@5 / MRR | TBD |
| Retrieval: RRF fusion | Recall@5 / MRR | TBD |
| Retrieval: RRF + rerank | Recall@5 / MRR | TBD |
| Diagnosis: LLM judge vs actual resolution | Agreement rate | TBD |
| Diagnosis: hand-checked sample (n=20) | Agreement with judge | TBD |
| Abstention | Rate, and accuracy when not abstaining | TBD |

---

## Stack

- **Python 3.12**, `uv`
- **PostgreSQL + pgvector**: tickets, chunks, embeddings, full-text search
- **Drain3**: log template mining
- **sentence-transformers**: `BAAI/bge-base-en-v1.5` embeddings, `bge-reranker-base` cross-encoder
- **Claude API**: diagnosis generation and LLM-judge eval
- **psycopg**, **pydantic**, **pytest**

---

## Project structure

```
loginsight/
├── src/loginsight/
│   ├── shared/          # used by both pipelines
│   │   ├── clean.py     #   Jira markup stripping, bot-comment filter
│   │   ├── extract.py   #   pull log lines and stack traces from code blocks
│   │   └── normalize.py #   masking + Drain template lookup
│   ├── indexing/        # Part 1
│   │   ├── logs/        #   HDFS reader, Drain training
│   │   ├── jira.py      #   fetch and load tickets, comments, links
│   │   ├── chunk.py
│   │   └── embed.py
│   ├── query/           # Part 2
│   │   ├── retrieve.py  #   dense, BM25, template match
│   │   ├── fuse.py      #   RRF + rerank
│   │   ├── diagnose.py  #   LLM diagnosis, abstention
│   │   └── post.py      #   Jira comment
│   └── eval/            # retrieval and diagnosis evaluation
├── scripts/             # entrypoints: build_index.py, triage.py, eval.py
├── tests/
└── data/            # gitignored: HDFS logs, raw Jira JSON
```

---

## Setup

```bash
git clone https://github.com/<you>/loginsight
cd loginsight
uv sync

docker compose up -d
docker compose exec -T db psql -U postgres -d loginsight -f - < scripts/schema.sql

mkdir -p data && cd data
curl -L "https://zenodo.org/records/8196385/files/HDFS_v1.zip?download=1" -o HDFS_v1.zip
unzip HDFS_v1.zip && cd ..
```

`.env`:
```
DATABASE_URL=postgresql://postgres:dev@localhost:5433/loginsight
DATA_DIR=./data
ANTHROPIC_API_KEY=...
```

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Skeleton, Docker, schema | ✅ |
| 1 | HDFS log ingestion, labels, stratified sample | ✅ |
| 2 | Drain parsing, masking, template accuracy | 🔄 |
| 3 | Jira ingestion: tickets, comments, links | ⬜ |
| 4 | Chunking, template extraction, embedding | ⬜ |
| 5 | Hybrid retrieval, temporal split, ablation | ⬜ |
| 6 | Triage pipeline: cited diagnosis, abstention | ⬜ |
| 7 | Diagnosis evaluation: LLM judge + hand check | ⬜ |
| 8 | Jira Cloud integration: webhook, auto-post | ⬜ |

---

## Key design decisions

**Temporal split, not random split.** A random split lets the index contain tickets resolved after the query was filed. That inflates recall and would never happen in production.

**Ground truth from engineers, not from me.** Retrieval labels come from issue links Hadoop engineers created while working the tickets. I didn't author any labels.

**Template match as its own retrieval signal.** Log snippets are where embeddings and keyword search both struggle. Normalizing them first, then matching on template IDs, is measured separately so its contribution shows up in the ablation.

**Abstention is a first-class outcome.** When retrieved evidence is weak, the pipeline says so instead of guessing. A confident wrong diagnosis on a production incident is worse than none.

**The LLM judge gets checked.** Diagnosis quality is scored by an LLM against the real resolution, then a hand-checked sample measures how far that judge can be trusted.
# triagerag

A RAG triage pipeline for distributed-system failures. When a new Jira ticket arrives, it normalizes any logs and stack traces in the ticket, retrieves related past tickets, and posts an LLM-generated diagnosis that cites its sources, or says it doesn't have enough evidence.

Built on real data: 13,302 resolved Apache HDFS Jira tickets as the knowledge base, and 11M lines of HDFS production logs for log-template vocabulary.

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

**1. Train the template model.** Drain streams all 11M HDFS log lines once and learns the templates. Masking rules replace variable values before mining, applied in order:

| Order | Mask | Example |
|---|---|---|
| 1 | `<IP>` | `10.251.43.21:50010` |
| 2 | `<BLK>` | `blk_-1608999687919862906` |
| 3 | `<EXC>` | `java.net.SocketTimeoutException: 480000 millis timeout...` |
| 4 | `<PATH>` | `/user/root/rand/_temporary/.../part-00590` |
| 5 | `<NUM>` | `67108864` |

Order matters: IPs must be masked before the path and number rules can break them into pieces. The similarity threshold is 0.85, strict enough to keep `PacketResponder ... terminating` (a normal shutdown) separate from `PacketResponder ... Interrupted.` (a failure). The trained model is saved to disk and reused everywhere after that.

**2. Fetch tickets.** Every resolved HDFS ticket (all types, not just bugs) comes from Apache's public Jira REST API, paged 100 at a time. Raw JSON is saved to `data/jira/raw_all/` first, so the API is hit once and every later step re-runs from disk.

**3. Load.** Tickets go into a `tickets` table with the full raw JSON kept alongside, and every issue link goes into `ticket_links`.

**4. Clean.** Jira wiki markup (`{code}`, `{noformat}`, `{{...}}`, non-breaking spaces) is stripped from prose. Comments from four bot accounts (`hudson`, `hadoopqa`, `githubbot`, `genericqa`) are dropped: together they posted 23,737 comments, more than the top 20 human contributors combined, almost all of it identical CI boilerplate.

**5. Extract and normalize logs.** Log lines and stack traces are pulled out of `{code}`/`{noformat}` blocks. Each log line is looked up against the trained Drain model (lookup only, the model never learns from tickets) to get a template ID, and the exception class (e.g. `java.net.SocketTimeoutException`) is extracted separately. The raw text stays in the chunk. The template IDs and exception classes are stored alongside it.

**6. Chunk.** Tickets are chunked by structure, not by fixed token windows:

| Chunk | Content | Why separate |
|-------|---------|--------------|
| Problem | Summary + description | How the issue was reported; this is what a new ticket looks like |
| Comment | One per substantive comment | Diagnosis happens in comments, often several replies in |
| Resolution | Final comments + fix summary | What actually fixed it; this is what the diagnosis cites |

Any chunk over ~500 tokens is split on paragraph boundaries with overlap. Every chunk has the ticket summary prepended before embedding, so a comment that just says "the lease recovery never ran" still carries its ticket context.

**7. Embed and store.** Chunks are embedded with `bge-base-en-v1.5` and written to Postgres: the vector to a pgvector HNSW index, the text to a `tsvector` GIN index for BM25, and the template IDs to a GIN array index.

### Part 2: Query

**1. Process the new ticket** with the same clean, extract, and normalize code from indexing.

**2. Retrieve from three sources in parallel**, all restricted to tickets resolved before the query ticket was created:
- **Dense**: cosine similarity on the embedding
- **BM25**: full-text rank, strong on exact error strings and class names
- **Template match**: overlap between the query's template IDs or exception classes and each chunk's

**3. Fuse** the three ranked lists with Reciprocal Rank Fusion.

**4. Rerank** the top 20 with a cross-encoder and keep the top 5.

**5. Diagnose.** Claude gets the ticket and the top chunks and writes a diagnosis that cites specific tickets. If the evidence is weak, it abstains.

**6. Post** the diagnosis as a Jira comment.

---

## Why normalize logs first

Two tickets describing the same failure rarely share text. Their stack traces contain different IPs, block IDs, ports, and timestamps. Embedding raw log text treats those as different, and keyword search misses them too.

Drain collapses log lines into templates. `Transmitted block blk_-1608999687919862906 to /10.251.65.203:50010` becomes `<IP>:Transmitted block <BLK> to /<IP>`. Once normalized, two tickets with the same failure match on template ID, regardless of which machine or block was involved. Template match is one of the retrieval signals, measured alongside dense and BM25.

---

## Data

**Apache HDFS Jira**: 13,302 resolved HDFS tickets of every type (bugs, improvements, tasks, duplicates), pulled from Apache's public Jira, including summary, description, comments, and 11,201 issue links. This is the retrieval corpus. The links were created by Hadoop engineers while working the tickets and serve as ground truth.

**HDFS v1 logs** from [LogHub](https://github.com/logpai/loghub): 11,175,629 lines over 38.7 hours from a real Hadoop cluster, with 29 ground-truth templates. Used to train and evaluate the Drain parser.

**Scope note:** the logs and the tickets come from different sources and don't describe the same incidents. The logs are used for template vocabulary and parser evaluation, not to diagnose specific tickets. Links to other Apache projects (HADOOP, HBASE, YARN, ...) are out of scope.

---

## Evaluation

### Query sets

Not every link is an answer. A link counts as a right answer for a test ticket only if:
1. its type is **Reference**, **Regression**, or **Problem/Incident** (planning links like Blocker or Required are excluded), and
2. the linked ticket was **resolved before the test ticket was filed**.

| | Count |
|---|---|
| Links in corpus | 11,201 |
| Test queries: fixed bugs filed after 2018-12-06 | 823 |
| **Main eval:** test queries with at least one valid answer | **163** |
| **Known-issue eval:** tickets closed as duplicates of a bug already fixed when they were filed | **21** |

The main set measures finding related past incidents. The known-issue set measures recognizing a re-reported, already-solved bug. With only 21 queries, known-issue results are reported as counts (e.g. 18/21), not percentages.

### Per-query temporal filter

For each test ticket, retrieval only searches tickets resolved before that ticket was created. This mirrors a live system: on the day a ticket arrives, it can search everything resolved up to that day, and never anything later. The query ticket itself is always excluded.

### Results

| Component | Metric | Result |
|-----------|--------|--------|
| Drain parsing | Templates vs 29 ground-truth | 31: 29 map 1:1, 2 split by argument count |
| Retrieval: dense only | Recall@5 / MRR | TBD |
| Retrieval: BM25 only | Recall@5 / MRR | TBD |
| Retrieval: template match only | Recall@5 / MRR | TBD |
| Retrieval: RRF fusion | Recall@5 / MRR | TBD |
| Retrieval: RRF + rerank | Recall@5 / MRR | TBD |
| Retrieval by link type | Recall@5 for Reference / Regression / Problem | TBD |
| Known-issue detection | Hits out of 21 | TBD |
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
- **httpx**, **psycopg**, **pydantic**, **pytest**

---

## Project structure

```
triagerag/
├── src/triagerag/
│   ├── shared/          # used by both pipelines
│   │   ├── clean.py     #   Jira markup stripping, bot-comment filter
│   │   ├── extract.py   #   pull log lines and stack traces from code blocks
│   │   └── normalize.py #   Drain template lookup + exception class
│   ├── index/           # Part 1
│   │   ├── logs/        #   HDFS reader, Drain training
│   │   ├── jira.py      #   fetch and load tickets and links
│   │   ├── chunk.py
│   │   └── embed.py
│   ├── query/           # Part 2
│   │   ├── retrieve.py  #   dense, BM25, template match
│   │   ├── fuse.py      #   RRF + rerank
│   │   ├── diagnose.py  #   LLM diagnosis, abstention
│   │   └── post.py      #   Jira comment
│   └── eval/            # retrieval and diagnosis evaluation
├── scripts/             # entrypoints: train_drain.py, fetch_jira.py, load_jira.py, ...
├── tests/
└── data/                # gitignored: HDFS logs, Drain state, raw Jira JSON
```

---

## Setup

```bash
git clone https://github.com/Parinita789/triagerag
cd triagerag
uv sync

docker compose up -d
docker compose exec -T db psql -U postgres -d triagerag -f - < scripts/schema.sql

# HDFS logs for Drain
mkdir -p data && cd data
curl -L "https://zenodo.org/records/8196385/files/HDFS_v1.zip?download=1" -o HDFS_v1.zip
unzip HDFS_v1.zip && cd ..
uv run python scripts/train_drain.py --retrain

# Jira tickets
uv run python scripts/fetch_jira.py
uv run python scripts/load_jira.py
```

`.env`:
```
DATABASE_URL=postgresql://postgres:dev@localhost:5433/triagerag
ANTHROPIC_API_KEY=...
```

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Skeleton, Docker, schema | ✅ |
| 1 | HDFS log reader | ✅ |
| 2 | Drain parsing, masking, template lookup | ✅ |
| 3 | Jira ingestion: tickets, links, eval query sets | ✅ |
| 4 | Cleaning, log extraction, chunking, embedding | ⬜ |
| 5 | Hybrid retrieval, temporal filter, ablation | ⬜ |
| 6 | Triage pipeline: cited diagnosis, abstention | ⬜ |
| 7 | Diagnosis evaluation: LLM judge + hand check | ⬜ |
| 8 | Jira Cloud integration: webhook, auto-post | ⬜ |

---

## Key design decisions

**Per-query temporal filter, not a global cutoff.** A single cutoff date hid tickets that were already resolved when a query was filed, leaving 11 usable queries. Filtering per query, by each ticket's own creation date, recovered them without leaking the future.

**Index every ticket type, not just fixed bugs.** Bugs link mostly to the improvements, tasks, and duplicates around them. The first corpus of 4,110 fixed bugs was missing most link targets. Widening to all 13,302 resolved tickets raised usable queries from 61 to 163, and it matches how an engineer actually searches.

**Duplicate links don't work as ground truth for fixed bugs.** A fixed bug's duplicates are filed after it, so the temporal filter correctly hides them. Using them would have leaked answers. Duplicates work only in the other direction, as the known-issue slice.

**Ground truth from engineers, not from me.** Every relevance label is an issue link Hadoop engineers created while working the tickets. I didn't author any labels.

**Exceptions masked in templates, kept as a separate field.** Exception wording changes between Hadoop versions and includes typos and varying timeout values. Keeping it inside the template would give the same failure different IDs. The template captures the event's structure, and the exception class is stored separately as its own signal.

**Template match as its own retrieval signal.** Log snippets are where embeddings and keyword search both struggle. Normalizing them first, then matching on template IDs, is measured separately so its contribution shows up in the ablation.

**Abstention is a first-class outcome.** When retrieved evidence is weak, the pipeline says so instead of guessing. A confident wrong diagnosis on a production incident is worse than none.

**The LLM judge gets checked.** Diagnosis quality is scored by an LLM against the real resolution, then a hand-checked sample measures how far that judge can be trusted.
# triagerag

An agentic RAG triage pipeline for distributed-system failures. When a new Jira ticket arrives, an LLM agent gathers evidence with tools: it queries logs in Loki, turns them into template statistics, and searches past tickets. Then it posts a diagnosis that cites its sources, or says it doesn't have enough evidence. No human is in the loop.

Built on real data: 13,302 resolved Apache HDFS Jira tickets as the knowledge base, and 11M lines of real HDFS cluster logs served from a local Loki instance.

---

## How it works

Two pipelines share one database, one redaction module, and one log-template model.

```
INDEXING (offline, run once, then incrementally)
  Jira API ──→ raw JSON ──→ clean ──→ redact ──→ extract logs ──→ normalize ──→ chunk ──→ embed ──→ Postgres
                                                     │                                              │
                                                     └──→ train template model (index tickets only) │
                                                                                                    │
QUERY (online, per new ticket)                                                                      │
  New ticket ──→ same clean / redact / extract / normalize                                          │
             ──→ ticket about past incidents:  search_past_tickets (no LLM) ──→ LLM ◀───────────────┘
             ──→ ticket about live behaviour:  LLM with log tools:
                   loki_labels · log_template_stats · log_lines   (Loki ──→ Drain templates vs baseline)
             ──→ diagnosis (cited, or abstain) ──→ redact output ──→ post to Jira
```

The query side reuses the indexing code for cleaning, redaction, extraction, and normalization. If a new ticket were processed differently from indexed tickets, their template IDs and pseudonyms wouldn't match and retrieval would quietly degrade.

### Part 1: Indexing

**1. Fetch tickets.** Every resolved HDFS ticket (all types, not just bugs) comes from Apache's public Jira REST API, paged 100 at a time. Raw JSON is saved to `data/jira/raw_all/` first, so the API is hit once and every later step re-runs from disk.

**2. Load.** Tickets go into a `tickets` table with the full raw JSON kept alongside, and every issue link goes into `ticket_links`.

**3. Clean.** Code and log blocks (`{code}`, `{noformat}`) are separated from prose and kept intact, indentation included, so log lines still match their templates. Blocks with an opening tag but no closing tag are recovered too. Jira wiki markup (`{{...}}`, `*bold*`, `h2.`, `[text|url]`, `[~user]`, images, non-breaking spaces) is stripped from the prose only.

Comments from four bot accounts (`hudson`, `hadoopqa`, `githubbot`, `genericqa`) are dropped: 60,692 comments, **34% of all comments**, almost all of it identical CI boilerplate. 116,685 human comments remain.

**4. Redact.** Secrets and PII are replaced with consistent pseudonyms in the summary, the prose, and every code block. Redaction runs last, on exactly the text that gets stored, so no later step can reassemble something the detectors never saw.

| Detector | How it decides |
|---|---|
| Private keys | Multi-line `BEGIN ... PRIVATE KEY` blocks, matched first |
| AWS keys, GitHub tokens, JWTs | Known prefix and length |
| Credentials | A keyword (`password=`, `token:`, ...) **and** a value that looks generated: 16+ chars, letters and digits, no code or placeholder characters, entropy > 3.5 |
| URL credentials | `user:pass@` inside URLs |
| Emails | Regex, with reserved documentation domains (`example.com`) allowlisted |
| Card numbers | 16 digits or 4 groups of 4 with one consistent separator, not glued to an ID, and passing Luhn |

Each value becomes `<KIND_xxxxxxxx>`, where the suffix is an HMAC of the value with a secret key. The same email always gets the same tag, so retrieval can still tell that three tickets involve the same person without ever seeing who. The tags can't be reversed without the key.

**5. Extract logs.** Log lines are found **by shape**, in both code blocks and prose: many tickets paste logs and stack traces straight into the text with no block around them. A line counts as a log line only if it has a timestamp, a level, and a `component:`, in one of the formats seen in the tickets:

| Format | Example |
|---|---|
| HDFS cluster log | `081109 203518 143 INFO dfs.DataNode$PacketResponder: ...` |
| Short date | `08/05/27 11:30:08 INFO mapred.JobClient: ...` |
| log4j default | `2013-02-14 17:29:58,128 ERROR org.apache...: ...` |

Exception lines (`java.io.IOException: ...`) are recorded by class. Stack frames are skipped: they change with every version. Lines with no real words after the prefix (empty messages, `-----` separators) are dropped.

Across all tickets this finds 7,670 log lines and 2,891 exception lines. **32% of the log lines were in prose, not code blocks**; scanning blocks only would have missed a third of them.

**6. Train the template model.** Drain learns log templates from the log lines extracted from **index tickets resolved before the 2018-12-06 cutoff**, so no test ticket's logs shape the vocabulary. Masking rules replace variable values before mining, applied in order:

| Order | Mask | Example |
|---|---|---|
| 1 | `<IP>` | `10.251.43.21:50010` |
| 2 | `<BLK>` | `blk_-1608999687919862906` |
| 3 | `<EXC>` | `java.net.SocketTimeoutException: 480000 millis timeout...` |
| 4 | `<PATH>` | `/user/root/rand/_temporary/.../part-00590` |
| 5 | `<NUM>` | `67108864` |

Order matters: IPs must be masked before the path and number rules can break them into pieces.

The similarity threshold was chosen by comparing three values on the ticket logs (6,202 training lines):

| sim_th | Templates | Seen once | Test lines matching a rare template |
|---|---|---|---|
| 0.85 | 2,012 | 57% | 24% |
| **0.75** | **1,706** | **50%** | **25%** |
| 0.65 | 1,532 | 49% | 27% |

0.65 matches the most, but only because it merges messages with opposite meanings: `Comparision result: [pass]` and `[fail]` become one template, and different CLI commands collapse into one. **0.75** reduces one-off templates without merging anything that means something different. About half the templates are seen only once at every threshold: ticket logs are scattered, so the data, not the threshold, is the limit.

The trained model is a build artifact, versioned in `models/drain_state.bin`, so the query side never needs the raw training data.

**7. Normalize.** Each extracted log line is looked up against the model (lookup only; the model never learns at query time) to get a template ID. The exception class is extracted separately, because exception wording changes between versions.

**8. Chunk.** Tickets are chunked by structure, not by fixed token windows:

| Chunk | Content | Count |
|-------|---------|-------|
| Problem | Summary + description | 13,528 |
| Comment | One per substantive human comment | 89,183 |

- Every chunk starts with `[HDFS-1234] summary`, so a comment that just says "the lease recovery never ran" still carries its ticket context.
- Code blocks stay in the text but are cut to 30 lines each; a 400-line stack trace would swamp the embedding. Exception classes and template IDs are stored as metadata on the chunk.
- Comments under 40 characters are skipped, and so are short sign-off comments (under 200 characters with commit or approval language like "+1, committed to trunk"). The sign-off filter removed 3,828 chunks.
- Anything over ~350 words is split on paragraph boundaries, with a short overlap.

A planned **resolution** chunk, built from each ticket's last two comments, was dropped after sampling: none of five sampled described a fix. In Apache projects the fix lives in the patch or pull request, and the last comments are usually review sign-off. For newer tickets, the GitHub PR description (posted by a bot account) is the real fix summary; it's a candidate source for a future fix section.

**9. Embed and store.** 102,711 chunks are embedded with `bge-base-en-v1.5` (768 dimensions, normalized) on an Apple-silicon GPU at ~62 chunks/s, about 28 minutes in total. Embedding runs 200 tickets at a time, committing each batch, so the build is resumable and a ticket is never half-indexed. Each chunk is written to Postgres with three indexes, one per retrieval method:

| Index | Column | Used by |
|---|---|---|
| HNSW | `embedding` | dense search |
| GIN | `tsv` (generated `tsvector`) | BM25-style keyword search |
| GIN | `template_ids` | template matching |

The HNSW index is built once, after the bulk load, instead of being updated on every insert. Queries must use the bge query prefix (`Represent this sentence for searching relevant passages: `); documents are embedded without it.

### Part 2: Query: retrieval first, then the LLM

A new ticket (a Jira webhook in production) goes through the same clean, redact, extract, and normalize steps. Then:

1. **Retrieval runs with no LLM.** The ticket's summary and description go straight into hybrid search; the top 5 past tickets are fetched deterministically. No model decides whether or what to search; every ticket gets searched.
2. **The LLM reads the results and writes the diagnosis.** It gets the ticket plus the top 5, and must call `submit_diagnosis`. In **pipeline mode** (`extra_calls = 0`, the default) that's one LLM call. In **hybrid mode** it may first spend up to N extra calls on its own searches or on reading a ticket in full.

The loop is written by hand against an OpenAI-compatible API (OpenRouter), with no agent framework.

**Tools:**

| Tool | What it does | Status |
|---|---|---|
| `search_past_tickets(query, k)` | Hybrid retrieval over past tickets (below), with a snippet from each ticket's best chunk | ✅ |
| `get_ticket(key)` | Full text of one past ticket. Refuses the query ticket itself and anything resolved after it | ✅ |
| `submit_diagnosis(...)` | The final answer: summary, likely cause, confidence, abstain, and cited tickets, each with a reason | ✅ |
| `loki_labels()` | Components and levels you can filter on, and the time span the logs cover | ✅ |
| `log_template_stats(start, end, block_id?, contains?, component?, level?)` | Fetches matching lines from Loki, matches each to a Drain template, and returns templates rarest first, each with the share of blocks that normally contain it (**RARE** under 2%). With `block_id`, also lists expected write-path templates that are **MISSING**. A 10-row summary instead of hundreds of raw lines | ✅ |
| `log_lines(start, end, ..., limit)` | Up to 50 raw lines, redacted, for context on one template | ✅ |

**The agent never writes LogQL.** It passes structured parameters and the code builds the query. A 27B model writing raw LogQL fails in many small ways, each of which would need its own validation rule. With structured parameters there are only values to check, injection isn't possible, and every query has a selector and a bounded range by construction. The trade-off: the agent can only ask what the parameters allow, which is enough for triage.

Log tickets skip the automatic ticket retrieval. "Problems with block X" retrieves noise from Jira, so the agent starts with the logs instead.

**Retrieval** (inside `search_past_tickets`) searches only chunks written before the query ticket was created, from tickets resolved before it:
- **Dense**: cosine similarity between the query embedding and each chunk's
- **BM25**: a hand-written BM25 over an in-memory inverted index; strong on exact class names, config keys, and error strings

Chunks are grouped into tickets (each ticket scored by its best chunk), and the two ranked ticket lists (top 50 each) are fused with Reciprocal Rank Fusion: `score = Σ 1 / (60 + rank)`. RRF uses ranks, not raw scores, so cosine similarities and BM25 scores never need to be put on the same scale.

A cross-encoder reranker and template-ID matching were both built and measured, and both were left out. See Evaluation.

**Guardrails**, which replace the checks a human would otherwise make:
- **Read-only.** No tool can change anything.
- **Tool arguments are validated with Pydantic** before anything runs. Invalid arguments go back to the model as an error result, so it can fix them.
- **Citations are checked against what the model actually saw.** A cited ticket that never appeared in a tool result is dropped. A diagnosis that doesn't abstain must cite at least one ticket, or it's rejected.
- **Same time filter as retrieval.** `get_ticket` refuses the query ticket and anything resolved after it, so the model can't read the future.
- **Hard limits:** `extra_calls + 3` LLM calls and 60k tokens per ticket; `max_tokens` 4096 per call, with reasoning effort set to low. The loop always terminates, with a `stopped` reason: `submitted`, `no_submit`, `budget`, or `llm_error`.
- **Tool errors are returned to the model as results,** never raised.
- **Log queries are bounded in code:** at most 6 hours, at most 4,000 lines (otherwise an error asks the agent to narrow the query), component and level checked against the values Loki actually has, and text filters free of quote characters. A block filter is a regex, `blk_-123([^0-9]|$)`, because a substring match on `blk_-123` would also pull in `blk_-1234...` and mix other blocks' lines into the evidence.
- **Template citations are checked like ticket citations:** a cited template must have appeared in a log tool result. After uncited citations are removed, the diagnosis is validated again, so one whose citations were all invented is rejected instead of passing with an empty list.
- **Every tool result is redacted** before the model sees it. `redact()` doesn't treat IP addresses as sensitive, and the log agent's diagnoses quote DataNode IPs. That's fine for this public data; for a private cluster, whether hostnames and IPs count as sensitive is a policy decision.
- **Every call is logged:** tool, arguments, result size, latency. That's the audit trail.
- **LLM calls:** 90 s timeout, retries with backoff on 429, connection errors, and 5xx, and a response cache keyed by a hash of the request, so evals resume where they stopped and reruns are free. Failed responses are never cached.

**Diagnosis.** The model writes a diagnosis that cites specific tickets, each with a reason. If the evidence is weak, it abstains. The diagnosis goes through `redact()` once more, since the LLM only saw pseudonyms but can still invent a plausible email or reconstruct something a detector missed. Then it's posted as a Jira comment (Phase 8). If the LLM fails, the retrieved tickets are still posted, without a diagnosis (Phase 8).

---

## Log evidence: two sources behind one interface

```python
class LogSource(Protocol):
    def evidence(self, ticket: CleanTicket) -> Extracted: ...
```

| Implementation | Where log lines come from | Used for |
|---|---|---|
| `PastedLogSource` | Log lines pasted into the ticket | **The retrieval eval**, on real Apache tickets |
| `LokiLogSource` | LogQL queries by the agent, over a time window | **The agent eval and demo**, on real HDFS cluster logs |

**Why two sources:** no public dataset has tickets and logs from the same system at the same time. The Apache tickets come from the whole Hadoop community over 19 years; the HDFS logs come from one cluster over 38 hours in 2008. So the ticket eval uses the logs people pasted into tickets, and the Loki path is evaluated on real, time-consistent cluster logs, with demo tickets about labeled blocks. Everything after the `LogSource` is the same code.

**How this looks in production:** each ticket would get a log fingerprint from Loki when it's created (the ticket's service and time window, normalized to templates, compared to a baseline) and the fingerprint would be stored on the ticket. It has to be stored at that moment, because Loki keeps logs for a limited time: a year-old ticket has no logs left to query, only its saved fingerprint.

**Loki setup notes:** loading 2008 logs into Loki 3.5 hits several defaults built for live logs.
- **`schema_config.from` must be before the data.** Loki rejects any line older than the first schema period. Example configs use a recent date, which silently rejects 2008 data.
- **`reject_old_samples: false`**, and ingestion rate limits raised from 4 MB/s to 64 MB/s for the bulk load.
- **Flush after loading.** Lines stay in the ingester's memory for up to 2 hours, and Loki only asks ingesters about recent time ranges. A 2008 query looked only in storage and came back empty until `POST /flush` wrote everything there.
- **Never reload flushed data into the same volume.** A test load of 200k lines followed by the full load left 83,594 duplicate lines in the 3 streams the test had covered: Loki only rejects a duplicate of the last line still in memory. Reloads start from an empty volume.
- **Turn off Loki's automatic labels** (`discover_service_name: []`, `discover_log_levels: false`), or every stream also gets `service_name` and `detected_level`.
- **Labels are `component` and `level` only.** HDFS v1 lines carry date, time, thread ID, level and component, and no host. The IPs in a message are the two ends of a transfer, not the machine writing the line. Block ID is deliberately not a label either: 575,000 values would create 575,000 streams.
- **Unique timestamps.** Each line gets `second × 10⁹ + its position within that second`, so lines keep file order and identical lines in the same second aren't dropped as duplicates.

Result: all **11,175,629 lines in 106 seconds, 14 streams, 0 rejected**. A per-stream count in LogQL matches the file exactly.

---

## Why normalize logs first

Two tickets describing the same failure rarely share text. Their stack traces contain different IPs, block IDs, ports, and timestamps. Embedding raw log text treats those as different, and keyword search misses them too.

Drain collapses log lines into templates. `Transmitted block blk_-1608999687919862906 to /10.251.65.203:50010` becomes `<IP>:Transmitted block <BLK> to /<IP>`. Once normalized, two tickets with the same failure match on template ID, regardless of which machine or block was involved.

---

## Data

**Apache HDFS Jira**: 13,302 resolved HDFS tickets of every type (bugs, improvements, tasks, duplicates), pulled from Apache's public Jira, including summary, description, comments, and 11,201 issue links. This is the retrieval corpus. The links were created by Hadoop engineers while working the tickets and serve as ground truth. Links to other Apache projects (HADOOP, HBASE, YARN, ...) are out of scope.

**HDFS v1 logs** from [LogHub](https://github.com/logpai/loghub): 11,175,629 lines over 38.7 hours from a real Hadoop cluster, with 29 ground-truth templates and labeled anomalous blocks. Used to validate the Drain configuration, and loaded into Loki for the agent eval and demo.

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

### What a query is

A query is what the ticket looked like **when it was filed**: its summary and description. Comments are excluded, because they're written later and often name the related ticket ("this looks like a regression from HDFS-1234"). Ticket keys like `HDFS-1234` are also stripped from the query, so retrieval can't find an answer by exact-matching a key the reporter already knew.

### Per-query temporal filter

For each query, retrieval only searches **chunks written before the query was created**, from **tickets resolved before it**. This mirrors a live system: on the day a ticket arrives, it can search everything up to that day, and never anything later. The query ticket itself is always excluded.

The first version filtered only on when each ticket was resolved. But comments can be added to a ticket after it's resolved, sometimes because of the newer ticket. Every chunk now carries its own creation time. Measured effect: one query of 163 had been found through a later comment.

### Dev and test sets

| Set | Queries | Used for |
|---|---|---|
| **Dev**: fixed bugs filed 2016-12-06 to 2018-12-06 | 160 | Choosing between variants |
| **Test**: fixed bugs filed after 2018-12-06 | 163 | Reporting final numbers, once |
| **Known-issue**: duplicates of an already-fixed bug, filed after 2018-12-06 | 21 | Reporting, once |

Variants were chosen on dev, with the decision rule written down before each run: adopt a variant only if it beats the current pipeline on Hit@5 with p < 0.05 on a two-sided sign test. The test set was used to report numbers, not to pick between them.

### Agent eval

**Ticket agent (Phase 6):** 12 dev tickets sampled with a fixed seed, run in pipeline mode and hybrid mode. It measures whether the LLM cites the linked ticket when retrieval found it, whether extra searches find linked tickets that retrieval missed, and cost and reliability.

**Log agent (Phase 7):** 23 tickets about single HDFS blocks, normal and anomalous mixed together, all with the same wording ("problems with block X around time T"). See [Log agent](#log-agent) below.

### Results

| Component | Metric | Result |
|-----------|--------|--------|
| Drain configuration | Templates on HDFS v1 vs 29 ground-truth | 31: 29 map 1:1, 2 split by argument count |
| Cleaning | Leftover markup after cleaning | 9 of ~130,000 texts, all typo'd tags |
| Redaction | Hits and sampled precision per detector | See below |
| Log extraction | Log lines found in tickets | 7,670 log lines in 777 tickets (6%); exception lines in 1,770 tickets (13%) |
| Template model | Test-ticket log lines matching a template learned from earlier tickets | 30% any template; 25% a rare template (seen in ≤20 tickets) |
| Retrieval | Hit@5, Recall@5, MRR@10 on 163 test queries | See below |
| Known-issue detection | Duplicate found in top 5 | **16/21** |
| Ticket agent: citation | Linked ticket cited when retrieval found it (12 dev tickets) | **8/8** |
| Ticket agent: extra searches | Linked tickets recovered that retrieval missed | **0/3** (hybrid, 8 of 12 tickets run) |
| Ticket agent: cost | LLM calls, tokens per ticket | pipeline **1.0 / 3.1k**; hybrid 2.6 / 9.0k |
| Log agent: anomalous blocks | Flagged, citing a template the block really has (13) | TBD |
| Log agent: normal blocks | Cleared without a false alarm (10, 5 of them with rare replication lines) | TBD |
| Diagnosis: LLM judge vs actual resolution | Agreement rate | TBD |
| Diagnosis: hand-checked sample (n=20) | Agreement with judge | TBD |
| Abstention | Rate, and accuracy when not abstaining | TBD |

### Retrieval

**Test set** (163 queries, 177 relevant links):

| Method | Hit@5 | Recall@5 | MRR@10 | Reference | Regression | Problem/Incident |
|---|---|---|---|---|---|---|
| Dense | 0.436 | 0.419 | 0.308 | 47/100 | 9/30 | 17/47 |
| BM25 | 0.417 | 0.405 | 0.307 | 46/100 | 8/30 | 17/47 |
| **RRF (dense + BM25)** | **0.479** | **0.460** | **0.360** | **54/100** | 8/30 | **19/47** |

Hit@5 is the share of queries with at least one linked ticket in the top 5. Recall@5 is the share of all linked tickets found in the top 5. MRR@10 rewards finding the first correct ticket early (rank 1 = 1.0, rank 2 = 0.5, ...).

**Why fusion helps:** dense and BM25 find different tickets. On the test set, 20 queries were hit only by dense and 17 only by BM25; a perfect combination of the two would reach 0.540. RRF reached 0.479. Compared with dense alone it gained 16 queries and lost 9. That gain is **not statistically significant** (sign test, p = 0.23), but it's consistent across all three metrics.

**Regression links are the hardest** (8–9 of 30 for every method). A regression links a symptom ("NameNode fails on startup") to the change that caused it ("refactor edit log loading"), and the two are usually described in different words.

**Variants tested on dev and left out:**

| Variant (dev, 160 queries) | Hit@5 | vs RRF | Decision |
|---|---|---|---|
| RRF | 0.575 | — | **kept** |
| RRF, then cross-encoder rerank of the top 20 | 0.487 | 10 gained, 24 lost, **p = 0.024** | dropped: significantly worse |
| RRF + reranker as a third vote | 0.575 | 3 gained, 3 lost, p = 1.0 | dropped: no gain at ~7× the latency |

`bge-reranker-base` was trained to judge whether a passage answers a question. This task is whether a past ticket is *related* to a new one, and a related ticket rarely answers the new one. The loss was concentrated in Reference links (65% → 51% on dev), the loosest kind of relation.

**Template-ID matching** was dropped from retrieval before measuring: only **3 of 163** test queries contain a log line, because bug reports describe symptoms in prose and logs get pasted later, in comments. The template model's job is summarizing live logs for the agent, where log lines number in the millions.

**Known limitation:** BM25's IDF (how rare each word is) is computed over the whole corpus, including tickets after each query. That's a leak of word statistics, not of any text, and standard in retrieval evals.

### Ticket agent

Model: `qwen/qwen3.8-27b:free` on OpenRouter (October 2026), temperature 0. 12 dev tickets, fixed seed.

| Mode | Tickets | Retrieval hit (top 5) | Cited by LLM | Found by LLM, missed by retrieval | Abstained | Tool errors | LLM calls | Tokens |
|---|---|---|---|---|---|---|---|---|
| **Pipeline** (`extra_calls = 0`) | 12 | 8/12 | **8/12** | — | 1 | 0 | **1.0** | **3,149** |
| Hybrid (`extra_calls = 3`) | 8 of 12* | 5/8 | 5/8 | **0/3** | 0 | 2 | 2.6 | 9,016 |

\*The free model's shared pool rate-limited the run. The remaining 4 tickets contain only 1 retrieval miss, so hybrid can't reach the 2-recovery bar set before the run.

**Pipeline mode is the default.** Hybrid behaved as designed: it read tickets in full, rewrote its queries, and sometimes cited extra related tickets. But it recovered none of the linked tickets that retrieval missed, at about 2.6× the calls and 2.8× the tokens. With 3 misses to recover and a 27B model, this is a small sample, not a general verdict on agentic retrieval.

**The LLM never lost a hit:** whenever retrieval found the linked ticket, the LLM cited it. The one abstention was on a ticket where retrieval had missed. In pipeline mode the LLM can only cite what retrieval found, so citation quality is capped by retrieval; diagnosis quality itself is measured in Phase 8.

**Bugs found by the eval:**

| Symptom | Cause | Fix |
|---|---|---|
| 0 of 6 cited, while retrieval hit 4 | The model listed tickets in its prose and left `related_tickets` empty | `related_tickets` made required in the schema; a validator rejects a non-abstaining diagnosis with no citations |
| 2 of 9 tickets hit the 60k token budget; 8 tool errors; 15.9k tokens per ticket | Runaway reasoning: one call had a 956-token prompt and generated 59,901 tokens of reasoning before the provider killed it. The failed response was also cached | `max_tokens` 4096 and reasoning effort low; failed responses no longer cached. Result: 0 tool errors, 1.0 calls and 3.1k tokens per ticket, a 5× cost cut |
| `submit_diagnosis({})` called with empty arguments | A model quirk: its own reasoning said "I need to actually fill in the parameters", then it sent `{}` again (10 cached cases) | The validator catches every case, and the model succeeds on the next call. A specific error message for empty arguments |
| Eval appeared stuck | Free-tier 429s from the model's shared upstream pool | Retries with backoff; the cache lets a stopped eval resume |

**Known limitations:** free OpenRouter endpoints may log prompts (acceptable for public Apache data, not for private tickets). Free-tier limits are 20 requests/min and 50/day under $10 of credit, and shared upstream pools return 429 regardless.

### Log agent

**There are no anomaly windows.** The plan was to write demo tickets for labeled anomaly windows and check whether the agent queried the right one. The labels are per block, though, and measured per hour, every hour of the 38 is 1–6% anomalous: hundreds of anomalous blocks an hour, no hour standing out. A "did it find the window" eval couldn't fail, so it would measure nothing. The eval is per block instead, the way real incident tickets name an ID (a request, a trace, here a block).

**There are no ERROR lines.** All 11M lines are INFO or WARN. Grepping for errors finds nothing; anomalies show up as rare templates, or as lines that should be there and aren't.

**Template model for the cluster.** A second Drain model, `models/drain_hdfs.bin`, is trained on the cluster's own logs: 31 templates, the same as the ground-truth check. The ticket model (trained on log lines pasted into tickets, from many Hadoop versions) matched only about 30% of later ticket logs and would leave most of these lines unmatched. Each log source gets its own template model. The tool's baseline, the share of blocks containing each template, is computed without labels.

**Signature templates, for scoring only.** From the labels: templates where most blocks containing them are anomalous. The agent and its tools never see these.

| | Templates | Examples | Anomalous share |
|---|---|---|---|
| Strong | 11 | `writeBlock <BLK> received exception <EXC>`, `Unexpected error trying to delete block <BLK>. BlockInfo not found in volumeMap`, `Redundant addStoredBlock request`, `Receiving empty packet for block` | ≥ 95% |
| Weak | 6 | `ask <IP> to replicate <BLK>`, `Transmitted block <BLK>`: re-replication, mostly a reaction to failure but also routine | 66–74% |

- **82.5% of anomalous blocks** (13,888 of 16,838) contain a signature. The weak replication templates also appear in 2,010 normal blocks, which is why RARE alone isn't proof.
- **Missing lines are a perfect signal for 6,181 blocks.** `PacketResponder ... terminating` is in 568,880 of 575,061 blocks, and every block without it is anomalous. A check for templates that are *present* can't see this, so the tool also reports expected write-path templates that are MISSING.
- **Some anomalies hide in the parameters.** The first anomalous block in the file has a perfect template lifecycle (3 of each write line), but its data went to one set of DataNodes while the NameNode registered a different set. The `<IP>` mask removes exactly that detail. Template analysis catches anomalies that change which events happen or how many; anomalies in values like host or size need a different check.

**Eval set** (`scripts/build_log_eval.py`, fixed seed, block lists sorted before sampling because Python randomizes set order per run):

| Group | Count | Correct if |
|---|---|---|
| Anomalous, strong signature (8 different signature types) | 10 | `anomaly_found` true, citing a strong signature the block has |
| Anomalous, missing write lines only | 3 | `anomaly_found` true, citing a missing write template |
| Normal, with replication lines | 5 | `anomaly_found` false, or true citing only weak templates at low confidence |
| Normal, plain | 5 | `anomaly_found` false |

The ticket time is when the first signature line appeared, which is when someone would notice. For `BlockInfo not found` that can be hours after the block was written, so the agent has to decide whether to look further back. Every ticket uses the same wording, so the agent has to work out which case it's in.

**Prompt disclosure:** the log prompt says "some rare lines record routine recovery work and also appear on healthy blocks". That's knowledge an HDFS engineer would have, but here it was learned from the labels, and it helps on the normal-replication blocks. An ablation without that line is a planned follow-up.

**First traces:** L02 (anomalous, exception) cited the exception and all three missing write lines, with a coherent cause: the stream read failed, so the write never completed or registered. L03 (normal, with replication) called the replication routine recovery and returned `anomaly_found=false`. L02 also sent `submit_diagnosis({})` three times in a row, the same empty-arguments quirk as in Phase 6, using 6 of its 7 allowed calls.

**Results:** TBD, run pending (free-tier daily limit).

### Redaction

Measured on all 12,788 ticket descriptions (9.4 MB). Precision was estimated by reading a random sample of hits for each detector, then fixing the detector and measuring again.

| Detector | First run | After fixes | Precision (sampled) | What changed |
|---|---|---|---|---|
| CARD | 125 | 0 | 0/10 before, all false positives | Block IDs, storage IDs, timestamps, file sizes. About 1 in 10 random numbers passes Luhn by chance, so a checksum alone isn't enough. Now requires real card formatting and word boundaries. |
| CREDENTIAL | 14 | 1 | 1/1 | 13 false positives were code (`fs.getDelegationToken(renewer)`), log text (`Token: No`), config key names, and placeholders. Now the value itself must look generated. |
| JWT | 2 | 2 | 2/2 | Both real signed-URL tokens. One payload decodes to an AWS access key ID, invisible to the AWS detector because it's base64-encoded. |
| EMAIL | 202 | 180 | ~7/10 real emails | `example.com` principals allowlisted. Remaining non-emails are shell prompts and Kerberos principals exposing internal hostnames, masked on purpose. |

Across descriptions and comments together: 759 emails and 2 credentials redacted.

**Throughput:** 11 MB/s on one core, about 25 core-hours per TB. A parallel batch backfill handles that easily; an NER model for names would be the bottleneck and would run on prose fields only.

**Known limitations:** secrets that are base64-encoded or compressed aren't detected. Short-lived HDFS checkpoint tokens (`token=-32:1989...:...`) pass through, because they contain no letters. Names aren't detected yet; that needs an NER model such as Presidio.

---

## Stack

- **Python 3.12**, `uv`
- **PostgreSQL + pgvector**: tickets, chunks, embeddings, full-text search
- **Loki**: log storage and LogQL, run locally in Docker
- **Drain3**: log template mining
- **sentence-transformers**: `BAAI/bge-base-en-v1.5` embeddings, `bge-reranker-base` cross-encoder
- **OpenRouter** via the `openai` SDK: hand-written tool-calling loop and diagnosis (model: `qwen/qwen3.8-27b:free`)
- **httpx**, **psycopg**, **pydantic**, **pytest**

---

## Project structure

```
triagerag/
├── src/triagerag/
│   ├── shared/            # used by both pipelines
│   │   ├── clean.py       #   code-block split, Jira markup stripping, bot-comment filter
│   │   ├── redact.py      #   secret and PII detection, HMAC pseudonyms
│   │   ├── extract.py     #   log lines and exceptions found by shape, in blocks and prose
│   │   └── normalize.py   #   template lookup + exception class
│   ├── index/             # Part 1
│   │   ├── logs/          #   HDFS reader, Drain training
│   │   ├── jira.py        #   fetch and load tickets and links
│   │   ├── chunk.py
│   │   └── embed.py
│   ├── query/             # Part 2
│   │   ├── retrieve.py    #   dense search, grouping chunks into tickets
│   │   ├── bm25.py        #   BM25 over an in-memory inverted index
│   │   ├── fuse.py        #   reciprocal rank fusion
│   │   ├── rerank.py      #   cross-encoder (measured, not used)
│   │   ├── search.py      #   SearchService: RRF search + get_ticket, time-filtered
│   │   ├── llm.py         #   OpenRouter client: timeout, retries, response cache
│   │   ├── logs.py        #   Loki tools: structured params -> LogQL, Drain templates vs baseline
│   │   ├── tools.py       #   tool schemas, Pydantic validation, citation check
│   │   ├── agent.py       #   retrieval first, then the LLM loop; limits, audit log
│   │   └── post.py        #   Jira comment
│   └── eval/              # retrieval, agent, and diagnosis evaluation
├── models/
│   ├── drain_state.bin             # template model trained on ticket logs, versioned in git
│   ├── drain_hdfs.bin              # template model trained on the HDFS cluster logs
│   └── hdfs_template_baseline.json # share of blocks containing each template (no labels)
├── scripts/               # entrypoints: fetch_jira.py, load_jira.py, train_drain.py,
│                          #   bench_redact.py, inspect_redact.py, check_clean.py, ...
├── tests/
├── loki-config.yaml
└── data/                  # gitignored: raw Jira JSON, HDFS logs
```

---

## Setup

```bash
git clone https://github.com/Parinita789/triagerag
cd triagerag
uv sync

docker compose up -d          # Postgres + Loki
docker compose exec -T db psql -U postgres -d triagerag -f - < scripts/schema.sql

# Jira tickets
uv run python scripts/fetch_jira.py
uv run python scripts/load_jira.py

# HDFS logs (for Loki and Drain validation)
mkdir -p data && cd data
curl -L "https://zenodo.org/records/8196385/files/HDFS_v1.zip?download=1" -o HDFS_v1.zip
unzip HDFS_v1.zip && cd ..
```

Logs into Loki, then the cluster template model and the log eval set:
```bash
uv run python scripts/load_loki.py          # 11M lines, ~2 min; flushes to storage at the end
uv run python scripts/loki_count.py         # per-stream counts should match the loader's
uv run python scripts/hdfs_signatures.py    # drain_hdfs.bin, baseline, signatures (~80 s)
uv run python scripts/build_log_eval.py     # data/eval/log_tickets.json
uv run python scripts/eval_log_agent.py --id L02   # one ticket, with trace
```
To reload Loki, remove the volume first (`docker compose rm -sf loki && docker volume rm triagerag_loki-data`); loading flushed data a second time creates duplicates.

The trained template model is committed in `models/`, so retraining is only needed after changing the masking rules or the threshold.

`.env`:
```
DATABASE_URL=postgresql://postgres:dev@localhost:5433/triagerag
LOKI_URL=http://localhost:3100
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=qwen/qwen3.8-27b:free
```

Run the agent:
```bash
uv run python scripts/run_agent.py HDFS-11445 --extra-calls 0   # one ticket, with trace
uv run python scripts/eval_agent.py --extra-calls 0             # 12 dev tickets
```

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Skeleton, Docker, schema | ✅ |
| 1 | HDFS log reader | ✅ |
| 2 | Drain configuration: masking, threshold, template lookup | ✅ |
| 3 | Jira ingestion: tickets, links, eval query sets | ✅ |
| 4 | Redaction, cleaning, log extraction, template model from ticket logs, chunking, embedding | ✅ |
| 5 | Hybrid retrieval, temporal filter, dev/test eval, reranker study | ✅ |
| 6 | Retrieval first, then an LLM loop written by hand: tools, validation, citation check, limits, audit log, pipeline vs hybrid eval | ✅ |
| 7 | Loki: 11M HDFS lines loaded, cluster template model, signature analysis, log tools with structured parameters, block-level eval set | 🔄 eval run pending |
| 8 | Diagnosis eval (LLM judge + hand check), Jira Cloud webhook and posting | ⬜ |

---

## Key design decisions

**Per-query temporal filter, not a global cutoff.** A single cutoff date hid tickets that were already resolved when a query was filed, leaving 11 usable queries. Filtering per query, by each ticket's own creation date, recovered them without leaking the future.

**Index every ticket type, not just fixed bugs.** Bugs link mostly to the improvements, tasks, and duplicates around them. The first corpus of 4,110 fixed bugs was missing most link targets. Widening to all 13,302 resolved tickets raised usable queries from 61 to 163, and it matches how an engineer actually searches.

**Duplicate links don't work as ground truth for fixed bugs.** A fixed bug's duplicates are filed after it, so the temporal filter correctly hides them. Using them would have leaked answers. Duplicates work only in the other direction, as the known-issue slice.

**Choose on dev, report on test.** Every variant was compared on a separate dev set, with the decision rule written down before seeing the result. Picking whichever variant scores best on the test set would quietly tune the system to those 163 queries.

**Report significance, not just differences.** With ~160 queries, a few points of Hit@5 can be noise. Every comparison reports a sign test on the queries where the two methods disagree; RRF's gain over dense is reported as consistent but not significant.

**A reranker isn't automatically an improvement.** The cross-encoder made retrieval significantly worse on dev. It was trained for question-answer relevance, and this task is ticket-to-ticket relatedness. It was measured and left out instead of included by default.

**Fuse on ranks, not scores.** Cosine similarity and BM25 scores are on unrelated scales. RRF combines rank positions, which avoids having to normalize scores at all.

**BM25 written by hand.** Postgres full-text ranking doesn't weight rare words above common ones, which is the core of BM25. A ~40-line implementation over an inverted index scores all 163 queries in milliseconds each.

**Timestamp every chunk.** Filtering by when a ticket was resolved isn't enough, because comments keep arriving afterwards. Each chunk carries its own creation time, and retrieval filters on it.

**Ground truth from engineers, not from me.** Every relevance label is an issue link Hadoop engineers created while working the tickets. I didn't author any labels.

**Don't embed raw logs.** Logs are huge, highly repetitive (11M HDFS lines collapse into 31 templates), and triage questions are about time, counts, exact IDs, and absence, which LogQL answers directly and vector similarity can't. The vector index holds tickets. Logs contribute template IDs and exception classes as chunk metadata, and live evidence comes from LogQL.

**Give the agent high-level tools, not raw access.** `log_template_stats` returns templates with counts against a baseline, not raw lines. An agent handed 50,000 lines drowns in them; an agent handed a 10-row summary sees the anomaly.

**Structured parameters, not model-written LogQL.** The code builds every query from a few validated values. See Part 2.

**Check the eval's premise before building it.** "Find the anomaly window" sounded right, but a one-minute measurement showed every hour is 1–6% anomalous. Without that check, the eval would have produced a number that measured nothing.

**Absence is evidence.** A third of the anomalous blocks are visible only through a missing line. Keyword search, embeddings, and "show me the rare templates" all miss them; a lifecycle check against what nearly every block contains finds them.

**One template model per log source.** Ticket logs span 19 years of Hadoop versions; the cluster logs are one version. Each gets its own model, and the tool's baseline is computed from the cluster's own logs, without labels.

**Rare isn't the same as wrong.** Re-replication templates are under 1% of blocks but a third of the time they're routine. The eval includes normal blocks with these lines, so an agent that calls every rare line the cause gets marked as a false alarm.

**Guardrails in code, not in the prompt.** Query validation, time-range and line limits, a tool-call budget, and redaction of every tool result are enforced by the tool layer. A prompt can ask the agent to behave; only code can guarantee it.

**The agent loop is written by hand.** Tool definitions, the call/result loop, retries, and limits use the OpenAI-compatible API directly, with no agent framework, so every step is visible and debuggable.

**Retrieval doesn't wait for an LLM.** The webhook triggers retrieval directly. A first LLM call only to decide "search for this ticket" adds cost and latency and a way to fail, for a decision that is always yes. The LLM comes in afterwards, to read the results and diagnose.

**Pipeline mode by default; agentic search had to earn its place.** Hybrid mode was measured against the one-call pipeline on the same tickets, with a recovery bar set before the run. It cost about 3× as much and recovered nothing, so it's off by default.

**Cap the reasoning, not only the loop.** A call budget doesn't stop a single call from generating 60k tokens of reasoning. `max_tokens` and a low reasoning effort cut cost 5× and removed every tool error in the eval.

**Validate citations in code.** A prompt asking for citations wasn't enough: the model named tickets in prose and left the structured field empty. The schema requires citations unless the model abstains, and cited tickets must have appeared in a tool result.

**Never cache a failure.** The response cache makes evals resumable and reruns free, but a cached error replays forever. Only successful responses are stored.

**Train templates on the tickets' own logs.** The 2008 cluster covers one component set in one version; the tickets span 19 years of HDFS. Templates are learned from log lines in index tickets resolved before the cutoff, which mirrors training on the logs of the systems the tickets are about. The HDFS logs validated the Drain configuration: 31 templates, 29 matching the ground truth one to one.

**Weight templates by how many tickets contain them, not by how many lines.** The biggest templates by line count are test-harness output pasted by a handful of tickets, and the most widespread are startup and shutdown lines like `SHUTDOWN_MSG`. Neither says anything about a specific failure. Template IDs are scored like words in BM25: a template found in few tickets counts a lot, one found in many counts for little. No hand-written blocklist.

**Template matching is a supporting signal, measured where it can help.** Only 6% of tickets contain log lines. On the full query set it can barely move recall, so its contribution is reported on the subset of queries that contain logs or exceptions.

**Find logs by shape, not only in code blocks.** Older tickets often paste logs and stack traces straight into the text. Scanning only `{code}` blocks would miss them, and those older tickets are the ones closest to the logs the model knows.

**Exceptions masked in templates, kept as a separate field.** Exception wording changes between Hadoop versions and includes typos and varying timeout values. Keeping it inside the template would give the same failure different IDs. The template captures the event's structure, and the exception class is stored separately as its own signal.

**The trained model is a versioned artifact.** `models/drain_state.bin` is committed to git. Anyone who clones the repo can triage without downloading 1.5GB of logs, and a retrain that changes the templates shows up as a diff.

**Redaction before indexing, before the LLM, and after it.** Apache Jira is public, but the pipeline is built for private ticket systems. Secrets and PII are replaced with pseudonyms before chunking, embedding, or any LLM call, on both paths, every tool result is redacted before the agent sees it, and the output is redacted again before it's posted. Secrets are never unmasked. PII isn't unmasked either: diagnoses cite tickets by key, and readers follow the link to the original in Jira, where Jira's own permissions apply.

**When unsure, redact.** The two kinds of error don't cost the same: masking a hostname loses a little retrieval signal, missing a secret is a leak that can't be undone. Detectors were tuned to remove noise (125 fake card numbers) but not to un-mask borderline infrastructure identifiers.

**Every real false positive becomes a test.** Each false positive found while inspecting hits on real tickets is a negative test case, so a future pattern change can't quietly bring it back.

**Raw ticket JSON is stored unredacted, here only.** The `tickets.raw` column keeps the original JSON because this data is public. A production deployment would store only redacted text and leave the original in Jira.

**Commenter usernames are kept.** Engineer names in internal tickets are useful context and usually not sensitive. Customer names would be. In production, name detection would allowlist the employee directory.

**A chunk's label has to match its content.** The planned resolution chunk was dropped because sampled text didn't describe fixes. A label that promises more than the text contains would mislead the diagnosis step, which cites "what fixed it".

**Load first, index after.** The HNSW index was dropped during the bulk load and built once at the end, with `maintenance_work_mem` raised for that session only. Updating the graph on every insert is far slower than building it in one pass.

**Filtered vector search needs care.** By default, HNSW finds the nearest candidates first and applies `WHERE` filters after. With a per-query time filter, a query can get far fewer than 5 results if its nearest neighbors are newer than it. The eval uses exact search to avoid this; the live path raises `hnsw.ef_search` or uses pgvector's iterative scan.

**Template match as its own retrieval signal.** Log snippets are where embeddings and keyword search both struggle. Normalizing them first, then matching on template IDs, is measured separately so its contribution shows up in the ablation.

**Abstention is a first-class outcome.** When evidence is weak, the pipeline says so instead of guessing. A confident wrong diagnosis on a production incident is worse than none.

**The LLM judge gets checked.** Diagnosis quality is scored by an LLM against the real resolution, then a hand-checked sample measures how far that judge can be trusted.
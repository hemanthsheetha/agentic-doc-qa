# agentic-doc-qa

Question answering over your documents with cited answers, running fully locally. It offers three retrieval
strategies over the same index:

| Strategy | How it works | Best for |
|---|---|---|
| **hybrid** | BM25 + vector search fused with RRF, top 8 sections to the LLM | Direct lookups ("What was Uber's 2021 revenue?") |
| **graph** | Hybrid seeds, then expansion along a context graph (cross-references, entity definitions, owners and roles, up to 2 hops) | Questions whose answer spans linked documents |
| **agent** | ReAct loop (think → tool → observe, max 8 steps) over `hybrid_search`, `follow_links`, `lookup_entity`, `read_section`, then an answer written from the full text of what it gathered | Multi-step questions where the next search depends on what was found |

Every answer cites its sources (`[n]` → page or `§section`). Citations to sources that weren't retrieved are removed.

**Stack:** FastAPI, Postgres with [ParadeDB `pg_search`](https://github.com/paradedb/paradedb) (BM25) and `pgvector`,
`Qwen3-Embedding-0.6B` (local), `qwen3:4b-instruct` via Ollama (local), and Streamlit.

## Architecture

![agentic-doc-qa architecture](docs/architecture.png)

**Ingestion** (1–3) runs as background jobs:
1. Files are parsed and chunked: PDFs per page, markdown per section.
2. Chunks are embedded on the GPU and upserted to Postgres. Unchanged chunks are skipped by hash.
3. For markdown, the context graph is rebuilt from cross-references and resolved entities.

**Queries** (4–7) also run as jobs:
4. The router picks a strategy.
5. Retrieval searches BM25 + vectors and, for graph or agent, follows graph links.
6. The local LLM writes the answer.
7. Citations to sources that weren't retrieved are dropped before the answer goes back.

Everything shares one 6 GB GPU, and `GPU_LOCK` keeps the embedder and the LLM from running at the same time.

## Results

Multi-hop set: `evals/golden_ops.jsonl`, 10 questions over 15 policy documents (tenant `ops`).

| | hybrid | graph | agent |
|---|---|---|---|
| Evidence recall (top 8 / observed) | 0.86 | 0.93 | **1.00** |
| Key facts in answer | 0.90 | **0.95** | **0.95** |
| LLM judge | **0.90** | 0.85 | 0.85 |
| Latency per answer | **4.4 s** | 5.4 s | 24.9 s |

Single-hop SEC filings (`evals/golden.jsonl`, hybrid search): recall@3 = 1.0, MRR = 0.94, key facts = 0.93.

On a laptop RTX 4050 (6 GB), ingest runs at about 30 chunks/s. With 10 questions, differences of about 0.05 are
within noise. The judge is the same local 4B model, so treat key-fact match as the primary signal.
`demos/graph-vs-hybrid.json` has two questions where graph and agent answer correctly and hybrid doesn't.

## Requirements

- Python 3.12, Docker, [Ollama](https://ollama.com)
- NVIDIA GPU with 6 GB or more is recommended (CPU works but ingest is slow)

## Setup

```bash
# 1. Postgres with pg_search + pgvector
docker run -d --name docqa-pg -e POSTGRES_USER=docqa -e POSTGRES_PASSWORD=docqa -e POSTGRES_DB=docqa \
  -p 5432:5432 -v docqa-pgdata:/var/lib/postgresql --restart unless-stopped paradedb/paradedb:latest
docker exec -i docqa-pg psql -U docqa -d docqa -v ON_ERROR_STOP=1 < db/schema.sql   # idempotent, also migrates

# 2. Python deps (torch with CUDA first)
pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt

# 3. LLM
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

## Ingest

```bash
python -m app.cli ingest                              # SEC PDFs in data/pdfs -> tenant "default"
python -m app.cli --tenant ops ingest-dir data/corpus # markdown policies -> tenant "ops" (+ context graph)
```

Metadata (company, form, period / title) is detected automatically. Uploads through the API or UI work too.

## Run

```bash
uvicorn app.api.main:app --port 8000                 # API, docs at http://localhost:8000/docs
streamlit run ui/hybrid.py --server.port 8501        # Filings Chat   (hybrid)
streamlit run ui/graph.py  --server.port 8502        # Policy Graph Chat (graph)
streamlit run ui/agent.py  --server.port 8503        # Agent Chat     (agent, with live trace)
```

From the terminal:

```bash
python -m app.cli --tenant ops ask "Which person, by name, must approve a Normal change to the Driver App?" --strategy agent
```

## API

| Method | Path | |
|---|---|---|
| GET | `/health` | Database and LLM status (503 if degraded) |
| GET | `/tenants/{t}/documents` | Indexed documents with chunk and page counts |
| POST | `/tenants/{t}/ingestion-jobs` | Ingest server-side files under `data/` (202, job) |
| POST | `/tenants/{t}/ingestion-jobs/uploads` | Upload PDF/markdown, metadata optional (202, job) |
| GET | `/tenants/{t}/ingestion-jobs/{id}` | Progress: done, failed, remaining |
| POST | `/tenants/{t}/query-jobs` | `{question, filters: {ticker, fiscal_year, doc_ids}, strategy}` (202, job) |
| GET | `/tenants/{t}/query-jobs/{id}` | Answer, citations, agent steps (live while running) |
| GET / POST | `/tenants/{t}/graph`, `/graph/rebuild` | Context graph stats / rebuild |

Data and jobs are isolated per tenant. The job store is in memory, so a restart loses job status but not ingested data.

## Evals and tests

```bash
python -m evals.run                                                    # SEC set, hybrid
python -m evals.run --golden evals/golden_ops.jsonl --tenant ops --strategy graph   # or hybrid / agent
python -m evals.run --retrieval-only                                   # fast, no LLM
pytest -q
```

Results are saved to `evals/results/` with the config used. To add a question, append a line to a `golden*.jsonl`
file: `question`, `filters`, `reference`, `facts` (accepted spellings), and `evidence` (`doc_id` plus text a
supporting chunk must contain).

## Layout

```
app/        chunking, documents (metadata), embeddings, store (ingest), retrieval (hybrid), generate (answers)
app/graph/  adapters (markdown -> graph), store, retriever (graph expansion)
app/agent/  tools, react (ReAct loop)
app/api/    FastAPI app, routers, job store
ui/         core.py (shared) + hybrid.py, graph.py, agent.py
db/         schema.sql
evals/      golden sets, metrics, runner
```

## Manual test: where graph and agent beat hybrid

Ask these on tenant `ops`, once in each app: Filings Chat (set **Workspace → Tenant** to `ops`), Policy Graph Chat,
and Agent Chat. Untick all documents. Answers vary in wording between runs, but the sources should not.

**1. "Which person, by name, must approve a Normal change to the Driver App, and how fast must its rollback plan complete?"**

- **Expected:** Sofia Martins (lead of the Edge team, which owns the Driver App), together with the Site Reliability
  Engineering team. The rollback must complete within 1 hour (the Tier 1 recovery time objective).
- **Hybrid:** names the role but not the person, and says the exact rollback time isn't in the sources. It never
  retrieves Team Directory §1 or Backup and Recovery Standard §2.
- **Graph / agent:** reach Team Directory §1 through *Edge team* (an entity link from Service Catalog §2.6), and Backup §2
  through the cross-reference in Change Management Policy §6. The agent trace shows `lookup_entity("Edge team")`.

**2. "Is a 20-day-old copy of the Billing Service data still available to restore, and what has to happen to the restored records afterwards?"**

- **Expected:** Yes. The Billing Service is Tier 2 and Tier 2 backups are kept for 35 days. Restored data must be
  re-purged to the Data Retention Policy periods within 30 days (customer invoices are kept for 7 years).
- **Hybrid:** says "yes" without a source for the 35 days. Its only backup source is Backup §1 (Purpose).
- **Graph / agent:** follow Data Retention Policy §2 → Backup and Recovery Standard §3 ("kept for 35 days") and justify the
  answer from it.

Recorded runs of both, with sources and full answers, are in `demos/graph-vs-hybrid.json`. For where the agent
specifically helps (aggregation, justified multi-step reasoning) and two agent demo questions, see
[docs/agent-evaluation.md](docs/agent-evaluation.md).

## Credits

Designed and directed by [Hemanth Sheetha](https://github.com/hemanthsheetha). All architecture, product and
evaluation decisions were his. These include the choice of strategies, the local model stack, the tenant and API
design, the UI, and what counts as good enough. Implementation was pair-programmed with Claude (Anthropic) as an AI
coding assistant.

## Configuration

Set through environment variables: `DATABASE_URL` (default `postgresql://docqa:docqa@localhost:5432/docqa`),
`OLLAMA_URL`, `LLM_MODEL`, `EMBED_MODEL`, `JUDGE_MODEL`, `API_URL` (UI), `INGEST_ROOT`, and `LOG_LEVEL`.

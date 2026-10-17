# CLAUDE.md

Local document QA with cited answers. There are three retrieval strategies over one index: **hybrid** (BM25 + vectors,
RRF), **graph** (hybrid seeds expanded along a context graph) and **agent** (a ReAct loop over search and graph tools).
Everything runs locally: Postgres (ParadeDB `pg_search` + `pgvector`), the Qwen3 embedder on the GPU, and Qwen3-4B via
Ollama. See README.md for setup and results.

## Environment

- Windows 11, PowerShell. Python is the conda env `retrieve-and-generate`:
  `D:\Anaconda\envs\retrieve-and-generate\python.exe` (Cursor's default interpreter is a different Python).
- Postgres runs in the Docker container `docqa-pg` on :5432 (`docqa`/`docqa`). Ollama runs on :11434.
- GPU is an RTX 4050 with **6 GB**, and RAM is tight. These constraints shaped the design; see Gotchas.
- Tenants: `default` = SEC filings in `data/pdfs`; `ops` = markdown policies in `data/corpus`, which have a graph.

## Commands

```bash
python -m pytest -q                                          # ~110 tests, ~20 s; DB tests skip if Postgres is down
uvicorn app.api.main:app --port 8000 --reload --reload-dir app   # --reload-dir: uploads in data/ must not restart it
streamlit run ui/hybrid.py --server.port 8501                # also ui/graph.py :8502, ui/agent.py :8503
python -m app.cli --tenant ops ask "<q>" --strategy agent    # hybrid | graph | agent | auto
python -m app.cli --tenant ops ingest-dir data/corpus        # rebuilds the tenant's graph for markdown
python -m evals.run --golden evals/golden_ops.jsonl --tenant ops --strategy graph   # --retrieval-only is fast
docker exec -i docqa-pg psql -U docqa -d docqa -v ON_ERROR_STOP=1 < db/schema.sql   # idempotent schema + migrations
```

Use a throwaway API on **:8001** for checks; the user's own API usually runs on :8000. Stop it afterwards.

## Architecture

```
app/chunking.py    PDF per page / markdown per section (recursive splitter, 2000 chars); Chunk model
app/documents.py   DocumentMetadata (kind sec_filing | document), cover-page detection, find/indexed_documents
app/embeddings.py  one model copy per device, never moved; GPU for bulk ingest, CPU for queries
app/store.py       ingest_file -> upsert_document (hash-skips unchanged chunks, deletes stale ones)
app/retrieval.py   hybrid_search (tenant-scoped SQL, RRF k=60)
app/generate.py    answer_question(strategy=...), prompt, build_citations (snippets + highlights)
app/graph/         adapters (deterministic MarkdownReferenceAdapter behind GraphAdapter), store, retriever
app/agent/         tools (numbered source registry, graph hints), react (loop + guardrails + synthesize)
app/api/           FastAPI: routers/*, schemas.py (request/response models), jobs.py (in-memory job store, pools)
ui/core.py         shared Streamlit UI; hybrid.py / graph.py / agent.py are UiConfig entry points
evals/             golden*.jsonl, metrics.py (pure functions), run.py
```

Data flow: API job → `store.ingest_file` → `chunks` / `documents` tables → graph rebuild if markdown. A query job calls
`answer_question`, which runs the chosen retrieval, then the LLM, then `build_citations`.

## Gotchas (each one has cost a debugging session)

- **VRAM:** GPU embedding (~3.3 GB peak) and the LLM (~3.9 GB) don't fit together. `GPU_LOCK` serializes them, and
  ingest calls `unload_all()` first. Never load models on the GPU in a long-lived process without releasing them
  (`release_gpu`).
- **Never move the embedder between devices.** `model.to("cpu")` OOM-ed system RAM. Load once per device.
- **No nested process pools.** Spawned workers re-import the main module on Windows; this froze the machine once.
- **Tenant scoping:** every SQL query filters `tenant_id`, and job reads check tenant. Keep it that way.
- **Empty filter is not "no filter":** `doc_ids=[]` returns nothing, and `None` means everything.
- **Schema changes** go in `db/schema.sql` as idempotent `ALTER ... IF NOT EXISTS`, placed directly after their table.
  Apply them to the running DB too.
- **pg_search 0.26:** use `text ||| q` and `pdb.score(id)`. `key_field` is a no-op. Temp tables with bm25 indexes spew
  warnings.
- **Streamlit:** escape `$` (it renders as LaTeX). Fragments poll only while a job is pending. Handle input *before*
  drawing the thread. `AppTest` can't attach files to `chat_input` or drive `file_uploader`.
- **Windows console:** set `PYTHONIOENCODING=utf-8` when printing emoji or `§`. Heredoc edits that contain `\n` in
  Python strings break; use the Edit tool.

## Conventions

- Match the surrounding style: small modules, type hints, pydantic models at boundaries, `logging` (no prints in
  `app/`), docstrings that explain *why*. Keep it simple and elegant; don't add abstractions speculatively.
- New behavior gets a test. Use stubs for the LLM, DB and models (see `tests/test_agent.py`, `tests/test_api.py`), plus
  DB-backed tests that skip when Postgres or the tenant is missing.
- Retrieval or prompt changes: run the relevant eval (`--strategy` for each one you touched) and report the numbers
  honestly, regressions included. With 10-question sets, ±0.05 is noise. Don't overfit the golden set.
- Golden questions: verify every fact and evidence string against the stored chunks before adding them. Evidence
  `contains` takes alternative wordings.

## Skills

Workflows live in `.claude/skills/`: `eval-and-compare`, `golden-questions`, `ingest-and-graph`, `ui-changes`,
`local-stack-debugging`, `commit-and-ship`. Use the matching one, and update it when you learn something new.

## Working with this user

- Just make must-have robustness fixes (memory, safety, correctness) and mention them. Ask only about real design
  choices or anything destructive.
- **Attribution:** AI assistance may be acknowledged, but always as *the user drove all decisions; Claude assisted
  with implementation* (see README → Credits). Never add a `Co-Authored-By: Claude` trailer to commits. Commit and
  push only when asked.
- Be concise and lead with results. Be candid about limitations: the local 4B model and the small eval sets.

---
name: ingest-and-graph
description: Add documents, corpora or tenants; change metadata, chunking or the DB schema; build or extend the context graph (adapters, entities, references). Use for anything touching ingestion or app/graph.
---

# Ingestion and the context graph

## Adding documents

- **PDFs** (SEC filings): metadata is detected from the cover page (`detect_metadata`): company, ticker, form, period.
  Verify detection against known metadata (there's a test per PDF in `tests/test_documents.py`).
- **Markdown:** title from the H1, one chunk per `##`/`###` section (`§3.1` maps to `sec:<doc>#3.1`), and the raw
  source is stored in `documents.source` because the graph needs empty parent headings.
- Ingest: `python -m app.cli --tenant <t> ingest-dir <folder>`, or upload through the API/UI. Markdown triggers
  `rebuild_graph` for the tenant. Unchanged chunks are skipped by content hash, which includes the model name.
- Changing the chunker or the embedder: re-ingest (it's safe in place; stale chunks get deleted), then run evals.

## Schema changes

- Edit `db/schema.sql`: the CREATE TABLE for fresh installs, plus an idempotent `ALTER ... IF NOT EXISTS` directly
  after that table, before any index that uses the new column.
- Apply it to the running DB, and run it twice to prove idempotency. Existing data must survive.

## Graph adapter (deterministic)

- `MarkdownReferenceAdapter` behind the `GraphAdapter` protocol (`build(docs) -> Graph`). New corpus rules go in
  `ReferenceConfig` (terms, headings, line patterns, aliases), not in new code paths.
- Edges: `references` ("Title §N", nearest-ancestor fallback; a title alone links to the document), `mentions`,
  `defined_in`, `owned_by` / `led_by` / `held_by`.
- **Always inspect a rebuilt graph** for false-positive entities (stopwords like "Each team"), look-alikes merging
  (Route Planner (legacy) vs Route Optimizer), and unresolved references. The real-corpus test requires every explicit
  `§` reference to resolve to a section.
- PDFs have no graph yet. A filing adapter would parse "Item 1A / Note N" headings and the ~220 "see Note N" references.

## Corpus design (when extending data/corpus)

- Mix explicit `§` references, implicit ones (a role without a name, "Tier 1" without a pointer), look-alike
  facts, and 3–4-document chains. Keep existing facts consistent (owners, tiers) across documents.

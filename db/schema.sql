-- Idempotent: safe to re-run, also upgrades existing databases (see "migrations" below).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

-- One row per document per tenant. Source of chunk context and of metadata filtering.
-- kind = 'sec_filing' (10-K/10-Q PDFs: SEC fields set) or 'document' (e.g. markdown policies: title set).
CREATE TABLE IF NOT EXISTS documents (
    tenant_id      text        NOT NULL,
    doc_id         text        NOT NULL,        -- file stem, e.g. "uber_10q_march_2022"
    kind           text        NOT NULL DEFAULT 'sec_filing' CHECK (kind IN ('sec_filing', 'document')),
    title          text,
    company        text,
    ticker         text,
    form           text        CHECK (form IN ('10-K', '10-Q')),
    fiscal_year    int,
    fiscal_quarter int         CHECK (fiscal_quarter BETWEEN 1 AND 4),
    period_end     date,
    source         text,       -- raw markdown for kind='document': input for the context graph
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, doc_id),
    CHECK ((form = '10-Q') = (fiscal_quarter IS NOT NULL))
);

-- migration for databases created before generic documents
ALTER TABLE documents ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'sec_filing';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source text;   -- raw markdown, input for the context graph
ALTER TABLE documents ALTER COLUMN company     DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN ticker      DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN form        DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN fiscal_year DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN period_end  DROP NOT NULL;

CREATE INDEX IF NOT EXISTS documents_filter_idx ON documents (tenant_id, ticker, fiscal_year);

CREATE TABLE IF NOT EXISTS chunks (
    id           bigint       GENERATED ALWAYS AS IDENTITY UNIQUE,  -- BM25 key (must be one unique column)
    tenant_id    text         NOT NULL,
    chunk_id     text         NOT NULL,         -- "lyft_2021:p12:c3" or "incident-response-runbook:s3.1:c0"
    doc_id       text         NOT NULL,
    page         int          CHECK (page >= 1),   -- PDFs
    section      text,                             -- markdown: "3.1"
    heading      text,                             -- markdown: "SEV1 escalation"
    context      text         NOT NULL,         -- "Lyft, Inc. (LYFT) Form 10-K, ..., page 12" / "Runbook §3.1 SEV1 escalation"
    text         text         NOT NULL,         -- raw chunk text, shown in citations
    content_hash text         NOT NULL,         -- sha256(model + context + text): skip re-embedding unchanged chunks
    embedding    vector(1024) NOT NULL,         -- Qwen/Qwen3-Embedding-0.6B over context + text, normalized
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, chunk_id),
    FOREIGN KEY (tenant_id, doc_id) REFERENCES documents (tenant_id, doc_id) ON DELETE CASCADE
);

-- migration for databases created before generic documents
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS section text;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS heading text;
ALTER TABLE chunks ALTER COLUMN page DROP NOT NULL;
DROP INDEX IF EXISTS chunks_doc_id_idx;

-- Per-document lookups (hash check, stale-chunk delete) and section lookups (graph expansion)
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (tenant_id, doc_id, section);

-- Semantic search
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Keyword search (BM25) over context and text; scores add up across both.
-- The unique key column (id) is listed first.
CREATE INDEX IF NOT EXISTS chunks_bm25
    ON chunks USING bm25 (id, context, text, tenant_id, doc_id, page);

-- Context graph (one per tenant, rebuilt from that tenant's markdown documents).
-- node_id: "doc:<doc_id>", "sec:<doc_id>#<section>", "ent:<type>:<slug>"
CREATE TABLE IF NOT EXISTS graph_nodes (
    tenant_id   text NOT NULL,
    node_id     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('document', 'section', 'entity')),
    label       text NOT NULL,
    doc_id      text,          -- document / section nodes
    section     text,          -- section nodes
    entity_type text,          -- entity nodes: service, team, person, role, severity, tier, term
    PRIMARY KEY (tenant_id, node_id)
);

-- kind: part_of, references, mentions, defined_in, owned_by, led_by
-- evidence: the text that produced the edge (e.g. "see the Customer Communication Guide §3")
CREATE TABLE IF NOT EXISTS graph_edges (
    tenant_id text NOT NULL,
    src       text NOT NULL,
    dst       text NOT NULL,
    kind      text NOT NULL,
    evidence  text,
    PRIMARY KEY (tenant_id, src, dst, kind),
    FOREIGN KEY (tenant_id, src) REFERENCES graph_nodes (tenant_id, node_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, dst) REFERENCES graph_nodes (tenant_id, node_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS graph_edges_src_idx ON graph_edges (tenant_id, src);

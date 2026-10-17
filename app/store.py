import logging
import os
import time
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from app.chunking import Chunk, ChunkerConfig, chunk_markdown, chunk_pdf
from app.documents import Document
from app.embeddings import content_hash, embed
from app.llm import GPU_LOCK, unload_all

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://docqa:docqa@localhost:5432/docqa")
DEFAULT_TENANT = "default"
CHUNKER_CONFIG = ChunkerConfig(strategy="recursive", max_chars=2000, overlap_ratio=0.2)

UPSERT_DOCUMENT_SQL = """
INSERT INTO documents (tenant_id, doc_id, kind, title, company, ticker, form, fiscal_year, fiscal_quarter, period_end,
                       source)
VALUES (%(tenant_id)s, %(doc_id)s, %(kind)s, %(title)s, %(company)s, %(ticker)s, %(form)s, %(fiscal_year)s,
        %(fiscal_quarter)s, %(period_end)s, %(source)s)
ON CONFLICT (tenant_id, doc_id) DO UPDATE SET
    kind           = EXCLUDED.kind,
    source         = EXCLUDED.source,
    title          = EXCLUDED.title,
    company        = EXCLUDED.company,
    ticker         = EXCLUDED.ticker,
    form           = EXCLUDED.form,
    fiscal_year    = EXCLUDED.fiscal_year,
    fiscal_quarter = EXCLUDED.fiscal_quarter,
    period_end     = EXCLUDED.period_end,
    updated_at     = now()
"""

UPSERT_CHUNK_SQL = """
INSERT INTO chunks (tenant_id, chunk_id, doc_id, page, section, heading, context, text, content_hash, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, chunk_id) DO UPDATE SET
    doc_id       = EXCLUDED.doc_id,
    page         = EXCLUDED.page,
    section      = EXCLUDED.section,
    heading      = EXCLUDED.heading,
    context      = EXCLUDED.context,
    text         = EXCLUDED.text,
    content_hash = EXCLUDED.content_hash,
    embedding    = EXCLUDED.embedding,
    updated_at   = now()
WHERE chunks.content_hash IS DISTINCT FROM EXCLUDED.content_hash
"""

DELETE_STALE_SQL = "DELETE FROM chunks WHERE tenant_id = %s AND doc_id = %s AND NOT (chunk_id = ANY(%s))"


def connect() -> psycopg.Connection:
    # autocommit: each conn.transaction() block is its own real transaction
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    register_vector(conn)
    logger.info("connected to postgres %s:%s/%s", conn.info.host, conn.info.port, conn.info.dbname)
    return conn


def upsert_document(conn: psycopg.Connection, tenant_id: str, doc: Document, chunks: list[Chunk],
                    source: str | None = None) -> int:
    """Upsert the document row, embed new/changed chunks, drop chunks no longer in the document.

    source: raw markdown, kept for building the context graph.
    """
    start = time.perf_counter()
    chunks = [c.model_copy(update={"context": doc.context(page=c.page, section=c.section, heading=c.heading)})
              for c in chunks]
    existing = dict(conn.execute(
        "SELECT chunk_id, content_hash FROM chunks WHERE tenant_id = %s AND doc_id = %s", (tenant_id, doc.doc_id)
    ).fetchall())
    changed = [c for c in chunks if existing.get(c.chunk_id) != content_hash(c.embed_text)]
    logger.info("%s/%s: %d chunks, %d already stored, %d new/changed",
                tenant_id, doc.doc_id, len(chunks), len(existing), len(changed))

    # Embed outside the transaction so a slow embedding run doesn't hold row locks.
    # GPU_LOCK keeps the LLM off the GPU while bulk embedding runs.
    vectors = []
    if changed:
        with GPU_LOCK:
            unload_all()
            vectors = embed([c.embed_text for c in changed], "RETRIEVAL_DOCUMENT")

    with conn.transaction():
        conn.execute(UPSERT_DOCUMENT_SQL, {"tenant_id": tenant_id, **doc.model_dump(), "source": source})
        with conn.cursor() as cur:
            cur.executemany(UPSERT_CHUNK_SQL, [
                (tenant_id, c.chunk_id, c.doc_id, c.page, c.section, c.heading, c.context, c.text,
                 content_hash(c.embed_text), v)
                for c, v in zip(changed, vectors)
            ])
        deleted = conn.execute(DELETE_STALE_SQL, (tenant_id, doc.doc_id, [c.chunk_id for c in chunks])).rowcount
    logger.info("%s/%s: upserted %d, removed %d stale in %.1fs",
                tenant_id, doc.doc_id, len(changed), deleted, time.perf_counter() - start)
    return len(changed)


def ingest_file(conn: psycopg.Connection, tenant_id: str, path: str | Path, doc: Document) -> int:
    """Chunk one PDF or markdown file and upsert it. Returns the number of chunks embedded."""
    is_markdown = Path(path).suffix.lower() == ".md"
    chunks = (chunk_markdown if is_markdown else chunk_pdf)(path, CHUNKER_CONFIG)
    if not chunks:
        raise ValueError(f"{Path(path).name}: no text to index" + ("" if is_markdown else " (scanned PDF?)"))
    source = Path(path).read_text(encoding="utf-8") if is_markdown else None
    return upsert_document(conn, tenant_id, doc, chunks, source)

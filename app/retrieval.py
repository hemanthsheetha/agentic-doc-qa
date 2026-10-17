import logging
import time

import psycopg

from app.chunking import Chunk
from app.embeddings import embed

logger = logging.getLogger(__name__)

RRF_K = 60

HYBRID_SQL = """
WITH semantic AS (
    SELECT id, row_number() OVER (ORDER BY dist) AS rank
    FROM (
        SELECT id, embedding <=> %(qvec)s AS dist
        FROM chunks
        WHERE tenant_id = %(tenant_id)s AND {doc_filter}
        ORDER BY dist
        LIMIT %(k)s
    ) s
),
keyword AS (
    SELECT id, row_number() OVER (ORDER BY bm25 DESC) AS rank
    FROM (
        SELECT id, pdb.score(id) AS bm25
        FROM chunks
        WHERE (context ||| %(qtext)s OR text ||| %(qtext)s)
          AND tenant_id = %(tenant_id)s AND {doc_filter}
        ORDER BY bm25 DESC
        LIMIT %(k)s
    ) k
)
SELECT c.chunk_id, c.doc_id, c.page, c.text,
       COALESCE(1.0 / (%(rrf_k)s + s.rank), 0)
     + COALESCE(1.0 / (%(rrf_k)s + k.rank), 0) AS score,
       s.rank AS semantic_rank, k.rank AS keyword_rank, c.context, c.section, c.heading
FROM semantic s
FULL OUTER JOIN keyword k USING (id)
JOIN chunks c USING (id)
ORDER BY score DESC
LIMIT %(top_n)s
"""


def hybrid_search(
    conn: psycopg.Connection,
    tenant_id: str,
    query: str,
    top_n: int = 10,
    k: int = 50,
    doc_ids: list[str] | None = None,
    qvec=None,
) -> list[tuple[Chunk, float]]:
    """BM25 + vector search over one tenant's chunks, fused with Reciprocal Rank Fusion.

    doc_ids=None searches all of the tenant's documents; an empty list (a filter
    that matched nothing) returns [].
    """
    if not query.strip():
        logger.warning("empty query, skipping search")
        return []
    if doc_ids is not None and not doc_ids:
        logger.warning("search %r: document filter matched nothing, returning no results", query[:80])
        return []
    start = time.perf_counter()
    if qvec is None:   # callers that also need the vector (graph retrieval) pass it in
        qvec = embed([query], "RETRIEVAL_QUERY")[0]
    embedded = time.perf_counter()
    sql = HYBRID_SQL.format(doc_filter="doc_id = ANY(%(doc_ids)s)" if doc_ids else "TRUE")
    params = {"qvec": qvec, "qtext": query, "tenant_id": tenant_id, "k": k,
              "rrf_k": RRF_K, "top_n": top_n, "doc_ids": doc_ids}

    with conn.transaction():
        # Settings are scoped to this transaction (is_local=true).
        conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(k, 40)),))
        # Tenant/doc filters apply after the HNSW scan; keep scanning until k rows survive them.
        conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        rows = conn.execute(sql, params).fetchall()

    both = sum(r[5] is not None and r[6] is not None for r in rows)
    logger.info(
        "search tenant=%s %r docs=%s: %d hits (%d both, %d semantic only, %d keyword only), embed %.0fms, db %.0fms",
        tenant_id, query[:80], doc_ids or "all", len(rows), both,
        sum(r[6] is None for r in rows), sum(r[5] is None for r in rows),
        (embedded - start) * 1000, (time.perf_counter() - embedded) * 1000,
    )
    if not rows:
        logger.warning("search %r returned nothing; is the tenant's data ingested?", query[:80])
    for r in rows:
        logger.debug("  %s score=%.5f semantic_rank=%s keyword_rank=%s", r[0], r[4], r[5], r[6])

    return [(Chunk(chunk_id=r[0], doc_id=r[1], page=r[2], text=r[3], context=r[7], section=r[8], heading=r[9]),
             float(r[4])) for r in rows]

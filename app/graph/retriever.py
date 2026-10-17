"""Multi-hop retrieval: hybrid search for seed sections, then follow the context graph.

1. Seeds: hybrid search (a separate tool), top SEEDS sections.
2. Expand: from seeds, follow explicit references and entity mentions (-> the entity's defining section,
   and through roles/owners, e.g. "Data Platform lead" -> Priya Raman -> Team Directory). Second hops
   continue only along explicit reference chains: a definition hub (a team directory, a glossary) links
   to everything, so hopping on from it mostly adds noise.
3. Score: seeds keep hybrid's ranking (BM25 + vectors), as a gentle rank decay 1.0, 0.97, 0.94, ...
   An expanded section *inherits* the score of the section pointing to it, times a per-edge weight:
   bridge sections rarely resemble the question, yet they are what multi-hop answers need.
4. Select: the top KEEP_SEEDS seeds are always kept; the remaining slots go to the best-scoring
   candidates, expansions and lower seeds alike.
"""
import logging
import time
from dataclasses import dataclass

import psycopg

from app.chunking import Chunk
from app.embeddings import embed
from app.graph.model import Graph, section_node
from app.graph.store import load_graph
from app.retrieval import hybrid_search

logger = logging.getLogger(__name__)

SEEDS = 8            # hybrid hits used as starting points (and as candidates)
KEEP_SEEDS = 4       # top seeds always kept in the context
MAX_HOPS = 2
SEED_DECAY = 0.97    # seed score = SEED_DECAY ** (hybrid rank - 1)
EDGE_WEIGHT = {"references": 0.95, "document": 0.95, "defined_in": 0.9, "related": 0.85}
CHAIN_EDGES = {"references", "document"}   # only these continue to a second hop


@dataclass
class GraphHit:
    chunk: Chunk          # a whole section (all its chunks joined), or a PDF chunk for non-markdown seeds
    score: float          # seed: rank decay; expansion: inherited score x edge weight
    hop: int              # 0 = found by hybrid search
    via: str | None       # why it was pulled in, e.g. "referenced from Incident Response Runbook §3.1"


def _sid(c: Chunk) -> str | None:
    return section_node(c.doc_id, c.section) if c.section else None


def _neighbors(g: Graph, node_id: str, entities: bool) -> list[tuple[str, str, str]]:
    """(target section/document node, edge type, reason) one hop from a section."""
    label = g.nodes[node_id].label
    out = []
    for e in g.out(node_id, "references"):
        kind = "document" if g.nodes[e.dst].kind == "document" else "references"
        out.append((e.dst, kind, f"referenced from {label}"))
    if not entities:
        return out
    for m in g.out(node_id, "mentions"):
        entity = g.nodes[m.dst]
        for d in g.out(m.dst, "defined_in"):
            out.append((d.dst, "defined_in", f"defines {entity.label}, mentioned in {label}"))
        for rel in g.out(m.dst, "held_by", "owned_by", "led_by"):   # e.g. role -> person, service -> team
            other = g.nodes[rel.dst]
            for d in g.out(rel.dst, "defined_in"):
                out.append((d.dst, "related", f"{entity.label} {rel.kind.replace('_', ' ')} {other.label}"))
    return out


def _load_sections(conn: psycopg.Connection, tenant_id: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], Chunk]:
    """Whole sections: all chunks of each (doc_id, section) joined in order."""
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT doc_id, section, heading, context, chunk_id, text FROM chunks "
        "WHERE tenant_id = %s AND (doc_id, section) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
        "ORDER BY doc_id, section, chunk_id",
        (tenant_id, [k[0] for k in keys], [k[1] for k in keys]),
    ).fetchall()
    out: dict[tuple[str, str], Chunk] = {}
    for doc_id, section, heading, context, chunk_id, text in rows:
        if (doc_id, section) in out:
            out[(doc_id, section)].text += "\n" + text
        else:
            out[(doc_id, section)] = Chunk(chunk_id=chunk_id, doc_id=doc_id, section=section, heading=heading,
                                           context=context, text=text)
    return out


def graph_search(
    conn: psycopg.Connection, tenant_id: str, question: str, top_n: int = 8, doc_ids: list[str] | None = None,
) -> list[GraphHit]:
    start = time.perf_counter()
    qvec = embed([question], "RETRIEVAL_QUERY")[0]
    seeds = hybrid_search(conn, tenant_id, question, top_n=SEEDS, doc_ids=doc_ids, qvec=qvec)
    g = load_graph(conn, tenant_id)
    if not g.nodes:
        logger.info("tenant=%s has no context graph; returning hybrid results", tenant_id)
        return [GraphHit(c, s, 0, None) for c, s in seeds[:top_n]]
    allowed = set(doc_ids) if doc_ids is not None else None

    # candidates: section node -> (score, hop, reason)
    seed_ids = list(dict.fromkeys(sid for c, _ in seeds if (sid := _sid(c)) and sid in g.nodes))
    cands: dict[str, tuple[float, int, str | None]] = {
        sid: (SEED_DECAY ** rank, 0, None) for rank, sid in enumerate(seed_ids)}
    frontier, doc_targets = list(seed_ids), {}
    for hop in range(1, MAX_HOPS + 1):
        nxt = []
        for sid in frontier:
            parent_score = cands[sid][0]
            for target, kind, reason in _neighbors(g, sid, entities=hop == 1):
                node = g.nodes[target]
                if allowed is not None and node.doc_id not in allowed:
                    continue
                cand = (parent_score * EDGE_WEIGHT[kind], hop, reason)
                if node.kind == "document":   # resolved below by a targeted search inside that document
                    doc_targets[node.doc_id] = max(doc_targets.get(node.doc_id, cand), cand, key=lambda t: t[0])
                elif cand[0] > cands.get(target, (0,))[0]:
                    cands[target] = cand
                    if kind in CHAIN_EDGES:
                        nxt.append(target)
        frontier = nxt

    if doc_targets:   # "see the Compensation Policy": the section of that document that fits the question
        for c, _ in hybrid_search(conn, tenant_id, question, top_n=3 * len(doc_targets),
                                  doc_ids=list(doc_targets), qvec=qvec):
            if (sid := _sid(c)) and c.doc_id in doc_targets and sid not in cands:
                cands[sid] = doc_targets.pop(c.doc_id)

    kept = seed_ids[:KEEP_SEEDS]
    pool = sorted((sid for sid in cands if sid not in kept), key=lambda sid: cands[sid][0], reverse=True)
    chosen = kept + pool[: max(0, top_n - len(kept))]

    key = {sid: (g.nodes[sid].doc_id, g.nodes[sid].section) for sid in chosen}
    sections = _load_sections(conn, tenant_id, list(key.values()))
    hits = [GraphHit(sections[key[sid]], *cands[sid]) for sid in chosen if key[sid] in sections]
    # non-markdown seeds (PDF chunks) have no section node: keep them as plain hybrid hits
    hits += [GraphHit(c, s, 0, None) for c, s in seeds if not _sid(c)][: max(0, top_n - len(hits))]

    logger.info("graph_search tenant=%s %r: %d seeds, %d candidates -> %d sections (%d expanded) in %.0fms",
                tenant_id, question[:80], len(seeds), len(cands), len(hits), sum(h.hop > 0 for h in hits),
                (time.perf_counter() - start) * 1000)
    for h in hits:
        logger.debug("  hop=%d %.3f %s %s", h.hop, h.score, h.chunk.context, h.via or "")
    return hits

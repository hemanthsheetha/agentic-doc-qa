import logging
import time

import psycopg

from app.chunking import parse_markdown
from app.graph.adapters import GraphAdapter, MarkdownReferenceAdapter
from app.graph.model import Graph, Node, SourceDocument, SourceSection

logger = logging.getLogger(__name__)


def load_sources(conn: psycopg.Connection, tenant_id: str) -> list[SourceDocument]:
    """The tenant's markdown documents, re-parsed from their stored source (keeps empty parent sections)."""
    rows = conn.execute(
        "SELECT doc_id, title, source FROM documents "
        "WHERE tenant_id = %s AND kind = 'document' AND source IS NOT NULL ORDER BY doc_id", (tenant_id,)
    ).fetchall()
    docs = []
    for doc_id, title, source in rows:
        parsed_title, sections = parse_markdown(source)
        docs.append(SourceDocument(doc_id, title or parsed_title or doc_id,
                                   [SourceSection(s.section, s.heading, s.text) for s in sections]))
    return docs


def save_graph(conn: psycopg.Connection, tenant_id: str, graph: Graph) -> None:
    """Replace the tenant's graph atomically (edges go with their nodes via ON DELETE CASCADE)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM graph_nodes WHERE tenant_id = %s", (tenant_id,))
        cur.executemany(
            "INSERT INTO graph_nodes (tenant_id, node_id, kind, label, doc_id, section, entity_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(tenant_id, n.node_id, n.kind, n.label, n.doc_id, n.section, n.entity_type) for n in graph.nodes.values()])
        cur.executemany(
            "INSERT INTO graph_edges (tenant_id, src, dst, kind, evidence) VALUES (%s, %s, %s, %s, %s)",
            [(tenant_id, e.src, e.dst, e.kind, e.evidence) for e in graph.edges.values()])


def load_graph(conn: psycopg.Connection, tenant_id: str) -> Graph:
    g = Graph()
    for node_id, kind, label, doc_id, section, entity_type in conn.execute(
            "SELECT node_id, kind, label, doc_id, section, entity_type FROM graph_nodes WHERE tenant_id = %s",
            (tenant_id,)):
        g.add_node(Node(node_id, kind, label, doc_id, section, entity_type))
    for src, dst, kind, evidence in conn.execute(
            "SELECT src, dst, kind, evidence FROM graph_edges WHERE tenant_id = %s", (tenant_id,)):
        g.add_edge(src, dst, kind, evidence)
    return g


def graph_stats(conn: psycopg.Connection, tenant_id: str) -> dict:
    """Counts of nodes by kind, entities by type, edges by kind."""
    nodes = dict(conn.execute("SELECT kind, count(*)::int FROM graph_nodes WHERE tenant_id = %s GROUP BY kind",
                              (tenant_id,)).fetchall())
    entities = dict(conn.execute("SELECT entity_type, count(*)::int FROM graph_nodes "
                                 "WHERE tenant_id = %s AND kind = 'entity' GROUP BY entity_type", (tenant_id,)).fetchall())
    edges = dict(conn.execute("SELECT kind, count(*)::int FROM graph_edges WHERE tenant_id = %s GROUP BY kind",
                              (tenant_id,)).fetchall())
    return {"built": bool(nodes), "nodes": nodes, "entities": entities, "edges": edges}


def rebuild_graph(conn: psycopg.Connection, tenant_id: str, adapter: GraphAdapter | None = None) -> dict[str, int]:
    """Build the tenant's graph from all its markdown documents and store it. Returns node/edge counts."""
    start = time.perf_counter()
    adapter = adapter or MarkdownReferenceAdapter()
    graph = adapter.build(load_sources(conn, tenant_id))
    save_graph(conn, tenant_id, graph)
    stats = graph.stats()
    logger.info("graph for tenant=%s rebuilt by %s in %.2fs: %s", tenant_id, adapter.name,
                time.perf_counter() - start, stats)
    return stats

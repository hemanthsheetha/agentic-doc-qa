from pathlib import Path

import psycopg
import pytest

from app.chunking import ChunkerConfig, chunk_markdown, parse_markdown
from app.graph.adapters import MarkdownReferenceAdapter
from app.graph.model import Graph, Node, SourceDocument, SourceSection

CORPUS = Path(__file__).resolve().parent.parent / "data" / "corpus"


def doc(doc_id, title, *sections):
    return SourceDocument(doc_id, title, [SourceSection(*s) for s in sections])


DOCS = [
    doc("runbook", "Incident Response Runbook",
        ("1", "Severity levels", "SEV1: full outage of a Tier 1 service."),
        ("3", "Escalation", ""),
        ("3.1", "SEV1 escalation", "Escalation contacts are in the On-call Handbook §2. Updates follow the Comms Guide."),
        ("4", "Data loss", "For data loss, page the Data Platform lead.")),
    doc("handbook", "On-call Handbook", ("2", "Escalation contacts", "Page the EM on duty. Each team rotates.")),
    doc("comms", "Comms Guide", ("3", "Cadence", "SEV1: first update within 30 minutes.")),
    doc("catalog", "Service Catalog",
        ("1", "Service tiers", "Tier 1 services are revenue-critical."),
        ("2", "Services", ""),
        ("2.1", "Route Optimizer", "Plans routes. Tier 1. Owner: Routing team."),
        ("2.2", "Route Planner (legacy)", "Old tool. Tier 3. Owner: Routing team.")),
    doc("directory", "Team Directory",
        ("1", "Engineering teams", "- Routing team: lead Marcus Chen.\n- Data Platform team: lead Priya Raman."),
        ("2", "Leadership", "- VP Engineering: Elena Petrova.")),
]


@pytest.fixture(scope="module")
def g() -> Graph:
    return MarkdownReferenceAdapter().build(DOCS)


def has(g, src, dst, kind):
    return (src, dst, kind) in g.edges


def test_explicit_section_reference(g):
    assert has(g, "sec:runbook#3.1", "sec:handbook#2", "references")


def test_title_without_section_references_the_document(g):
    assert has(g, "sec:runbook#3.1", "doc:comms", "references")


def test_missing_section_falls_back_to_ancestor():
    g = MarkdownReferenceAdapter().build(DOCS + [doc("x", "X Policy", ("1", "Scope", "See the Service Catalog §2.9."))])
    assert has(g, "sec:x#1", "sec:catalog#2", "references")


def test_fixed_vocabulary_entities_are_defined_by_heading(g):
    assert has(g, "ent:severity:sev1", "sec:runbook#1", "defined_in")
    assert has(g, "ent:tier:tier-1", "sec:catalog#1", "defined_in")
    assert has(g, "sec:comms#3", "ent:severity:sev1", "mentions")
    assert not has(g, "sec:runbook#1", "ent:severity:sev1", "mentions")   # not a mention of itself


def test_services_come_from_catalog_subsections_with_owners(g):
    assert has(g, "ent:service:route-optimizer", "sec:catalog#2.1", "defined_in")
    assert has(g, "ent:service:route-optimizer", "ent:team:routing-team", "owned_by")
    assert "ent:service:route-planner-legacy" in g.nodes


def test_lookalike_service_names_stay_apart(g):
    # "Route Planner (legacy)" must not be resolved as a mention of the Route Optimizer, and vice versa
    assert not has(g, "sec:catalog#2.2", "ent:service:route-optimizer", "mentions")


def test_roles_resolve_to_people(g):
    assert has(g, "ent:role:data-platform-lead", "ent:person:priya-raman", "held_by")
    assert has(g, "sec:runbook#4", "ent:role:data-platform-lead", "mentions")
    assert has(g, "ent:role:vp-engineering", "ent:person:elena-petrova", "held_by")


def test_team_stopwords_are_not_entities(g):
    assert not any(n.label.lower().startswith("each") for n in g.nodes.values())


def test_graph_drops_dangling_and_self_edges():
    g = Graph()
    g.add_node(Node("a", "section", "A"))
    g.add_edge("a", "a", "references")
    g.add_edge("a", "missing", "references")
    assert not g.edges


def test_parse_markdown_sections_and_preamble():
    title, sections = parse_markdown("# Doc\n\nIntro text.\n\n## 1. One\n\nBody.\n\n### 1.1 Sub\n\nMore.\n\n## Glossary\n\nTerms.")
    assert title == "Doc"
    assert [(s.section, s.heading, s.text) for s in sections] == [
        ("0", "Overview", "Intro text."), ("1", "One", "Body."), ("1.1", "Sub", "More."), ("glossary", "Glossary", "Terms.")]


def test_chunk_markdown_ids_map_to_sections():
    chunks = chunk_markdown(CORPUS / "incident-response-runbook.md", ChunkerConfig())
    assert "incident-response-runbook:s3.1:c0" in {c.chunk_id for c in chunks}
    assert all(c.section and c.page is None for c in chunks)


def test_real_corpus_references_all_resolve():
    """Every '<Title> §N' in the corpus becomes a reference to a real section (none fall back to a document)."""
    import re
    docs = []
    for path in sorted(CORPUS.glob("*.md")):
        title, sections = parse_markdown(path.read_text(encoding="utf-8"))
        docs.append(SourceDocument(path.stem, title, [SourceSection(s.section, s.heading, s.text) for s in sections]))
    g = MarkdownReferenceAdapter().build(docs)
    explicit = sum(len(re.findall(r"§\s*\d", s.text)) for d in docs for s in d.sections)
    to_sections = [e for e in g.edges.values() if e.kind == "references" and e.dst.startswith("sec:")]
    assert len(to_sections) == explicit


# --- retriever, against the ingested 'ops' tenant (skipped if not available) ---------

@pytest.fixture(scope="module")
def ops_conn():
    from app.store import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=3)
        if not conn.execute("SELECT EXISTS (SELECT 1 FROM graph_nodes WHERE tenant_id = 'ops')").fetchone()[0]:
            pytest.skip("ops tenant has no graph; run: python -m app.cli --tenant ops ingest-dir data/corpus")
    except psycopg.Error:
        pytest.skip("postgres not reachable or schema not migrated")
    from pgvector.psycopg import register_vector
    register_vector(conn)
    yield conn
    conn.close()


def test_graph_search_pulls_in_the_bridge_section(ops_conn):
    from app.graph.retriever import graph_search
    hits = graph_search(ops_conn, "ops", "We lost customer data. Which specific person must be paged besides the usual escalation?")
    found = {(h.chunk.doc_id, h.chunk.section): h for h in hits}
    bridge = found.get(("team-directory", "1"))
    assert bridge and bridge.hop >= 1 and "Data Platform lead" in bridge.via
    assert "Priya Raman" in bridge.chunk.text

"""Graph adapters: turn a tenant's source documents into a context graph.

Adapters are pluggable: anything with `name` and `build(documents) -> Graph` works (e.g. a future
LLM-extraction adapter). MarkdownReferenceAdapter is deterministic: entity resolution by rules.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from app.graph.model import Graph, Node, SourceDocument, doc_node, entity_node, section_node

logger = logging.getLogger(__name__)


class GraphAdapter(Protocol):
    name: str

    def build(self, documents: list[SourceDocument]) -> Graph: ...


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def sentence_at(text: str, start: int, end: int, limit: int = 240) -> str:
    """The sentence (or line) around a match, as edge evidence."""
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start))
    right = min([i for i in (text.find(". ", end), text.find("\n", end)) if i != -1] or [len(text)])
    return " ".join(text[left + 1: right + 1].split())[:limit]


@dataclass
class TermSpec:
    """A fixed vocabulary entity, e.g. SEV1, defined in the section whose heading matches `defined_heading`."""
    entity_type: str
    label: str
    pattern: str
    defined_heading: str


def default_terms() -> list[TermSpec]:
    terms = [TermSpec("severity", f"SEV{n}", rf"\bSEV\s?{n}\b", r"^severity levels$") for n in range(1, 5)]
    terms += [TermSpec("tier", f"Tier {n}", rf"\bTier\s?{n}\b", r"^service tiers$") for n in range(1, 4)]
    terms += [
        TermSpec("term", "RPO", r"\b(?:RPO|recovery point objectives?)\b", r"^recovery objectives$"),
        TermSpec("term", "RTO", r"\b(?:RTO|recovery time objectives?)\b", r"^recovery objectives$"),
    ]
    return terms


@dataclass
class ReferenceConfig:
    """Rules for the deterministic adapter; swap them per corpus."""
    terms: list[TermSpec] = field(default_factory=default_terms)
    services_heading: str = r"^services$"            # children of this section are services
    owner_pattern: str = r"Owner:\s*(?:the\s+)?(?P<team>(?:[A-Z][\w-]*\s)+?)team\b"
    team_pattern: str = r"\b(?P<name>(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s(?:[A-Z][a-z]+|[A-Z]{2,})){0,3})\steam\b"
    team_line: str = r"^-\s*(?P<team>[A-Z][\w ]+?) team:\s*(?:lead\s+(?P<lead>[A-Z][a-z]+(?:\s[A-Z][a-z]+)+))?"
    role_line: str = r"^-\s*(?P<role>[A-Z][A-Za-z ]+?):\s*(?P<person>[A-Z][a-z]+\s[A-Z][a-z]+)\b"
    stopwords: frozenset = frozenset({"The", "Each", "Every", "This", "That", "Our", "A", "An", "Any", "Owning"})
    aliases: dict[str, str] = field(default_factory=lambda: {   # alias -> canonical entity label
        "SRE": "Site Reliability Engineering team",
        "Site Reliability Engineering": "Site Reliability Engineering team",
    })


@dataclass
class _Entity:
    node_id: str
    label: str
    entity_type: str
    patterns: list[str]
    defined_in: str | None = None


class MarkdownReferenceAdapter:
    """Sections become nodes; references and resolved entity mentions become edges.

    - "<Document Title> §N"      -> section references section (nearest existing ancestor if §N is missing)
    - "<Document Title>" (no §)  -> section references document
    - services / teams / people / roles / severities / tiers / terms -> section mentions entity,
      entity defined_in its defining section, plus owned_by / led_by / held_by between entities
    """
    name = "markdown-references"

    def __init__(self, config: ReferenceConfig | None = None):
        self.cfg = config or ReferenceConfig()

    def build(self, documents: list[SourceDocument]) -> Graph:
        g = Graph()
        self._structure(g, documents)
        self._references(g, documents)
        entities = self._entities(g, documents)
        self._mentions(g, documents, entities)
        logger.info("graph built by %s from %d documents: %s", self.name, len(documents), g.stats())
        return g

    # --- structure -----------------------------------------------------------------

    def _structure(self, g: Graph, documents: list[SourceDocument]) -> None:
        for d in documents:
            g.add_node(Node(doc_node(d.doc_id), "document", d.title, doc_id=d.doc_id))
            for s in d.sections:
                g.add_node(Node(section_node(d.doc_id, s.section), "section", f"{d.title} §{s.section} {s.heading}",
                                doc_id=d.doc_id, section=s.section))
            for s in d.sections:
                parent = s.section.rsplit(".", 1)[0] if "." in s.section else None
                target = section_node(d.doc_id, parent) if parent and section_node(d.doc_id, parent) in g.nodes \
                    else doc_node(d.doc_id)
                g.add_edge(section_node(d.doc_id, s.section), target, "part_of")

    # --- explicit references ---------------------------------------------------------

    def _references(self, g: Graph, documents: list[SourceDocument]) -> None:
        by_title = {d.title.lower(): d.doc_id for d in documents}
        titles = "|".join(re.escape(t) for t in sorted(by_title, key=len, reverse=True))
        if not titles:
            return
        ref_re = re.compile(rf"(?P<title>{titles})(?:\s*§\s*(?P<sec>\d+(?:\.\d+)*))?", re.I)
        for d in documents:
            for s in d.sections:
                src = section_node(d.doc_id, s.section)
                for m in ref_re.finditer(s.text):
                    target_doc = by_title[m.group("title").lower()]
                    evidence = sentence_at(s.text, m.start(), m.end())
                    if m.group("sec"):
                        g.add_edge(src, self._resolve_section(g, target_doc, m.group("sec")), "references", evidence)
                    elif target_doc != d.doc_id:
                        g.add_edge(src, doc_node(target_doc), "references", evidence)

    @staticmethod
    def _resolve_section(g: Graph, doc_id: str, section: str) -> str:
        """§3.4 -> §3.4 if it exists, else §3, else the document."""
        parts = section.split(".")
        while parts:
            node = section_node(doc_id, ".".join(parts))
            if node in g.nodes:
                return node
            parts.pop()
        return doc_node(doc_id)

    # --- entity resolution -----------------------------------------------------------

    def _entities(self, g: Graph, documents: list[SourceDocument]) -> dict[str, _Entity]:
        cfg, ents = self.cfg, {}

        def add(entity_type: str, label: str, patterns: list[str], defined_in: str | None = None) -> _Entity:
            nid = entity_node(entity_type, slug(label))
            e = ents.setdefault(nid, _Entity(nid, label, entity_type, patterns))
            e.defined_in = e.defined_in or defined_in
            g.add_node(Node(nid, "entity", label, entity_type=entity_type))
            return e

        sections = [(d, s, section_node(d.doc_id, s.section)) for d in documents for s in d.sections]

        for t in cfg.terms:   # fixed vocabularies
            defined = next((sid for _, s, sid in sections if re.search(t.defined_heading, s.heading, re.I)), None)
            add(t.entity_type, t.label, [t.pattern], defined)

        for d in documents:   # services: sub-sections of the "Services" section
            parents = {s.section for s in d.sections if re.search(cfg.services_heading, s.heading, re.I)}
            for s in d.sections:
                if "." in s.section and s.section.rsplit(".", 1)[0] in parents:
                    plain = re.sub(r"\s*\(.*?\)", "", s.heading).strip()
                    pats = [rf"\b{re.escape(s.heading)}"] + ([rf"\b{re.escape(plain)}\b(?!\s*\()"] if plain != s.heading else [])
                    add("service", s.heading, pats, section_node(d.doc_id, s.section))

        for d, s, sid in sections:   # teams, leads, roles and people from directory-style lines
            for line in s.text.splitlines():
                if m := re.match(cfg.team_line, line):
                    team = add("team", f"{m.group('team')} team", [rf"\b{re.escape(m.group('team'))} team\b"], sid)
                    if m.group("lead"):
                        person = add("person", m.group("lead"), [rf"\b{re.escape(m.group('lead'))}\b"], sid)
                        role = add("role", f"{m.group('team')} lead", [rf"\b{re.escape(m.group('team'))} lead\b"], sid)
                        g.add_edge(team.node_id, person.node_id, "led_by", line.strip("- ").strip())
                        g.add_edge(role.node_id, person.node_id, "held_by", line.strip("- ").strip())
                elif m := re.match(cfg.role_line, line):
                    person = add("person", m.group("person"), [rf"\b{re.escape(m.group('person'))}\b"], sid)
                    role = add("role", m.group("role"), [rf"\b(?:the\s)?{re.escape(m.group('role'))}\b"], sid)
                    g.add_edge(role.node_id, person.node_id, "held_by", line.strip("- ").strip())

        for d, s, sid in sections:   # teams mentioned anywhere, even if not in a directory
            for m in re.finditer(cfg.team_pattern, s.text):
                words = m.group("name").split()
                while words and words[0] in cfg.stopwords:
                    words.pop(0)
                if words:
                    name = " ".join(words)
                    add("team", f"{name} team", [rf"\b{re.escape(name)} team\b"])

        for alias, canonical in cfg.aliases.items():   # extra spellings resolve to existing entities
            for e in ents.values():
                if e.label == canonical:
                    e.patterns.append(rf"\b{re.escape(alias)}\b(?!\s+team)")

        for d, s, sid in sections:   # ownership: "Owner: Edge team" in a service section
            if m := re.search(cfg.owner_pattern, s.text):
                service = next((e for e in ents.values() if e.entity_type == "service" and e.defined_in == sid), None)
                team = ents.get(entity_node("team", slug(f"{m.group('team').strip()} team")))
                if service and team:
                    g.add_edge(service.node_id, team.node_id, "owned_by", sentence_at(s.text, m.start(), m.end()))

        for e in ents.values():
            if e.defined_in:
                g.add_edge(e.node_id, e.defined_in, "defined_in")
        return ents

    def _mentions(self, g: Graph, documents: list[SourceDocument], ents: dict[str, _Entity]) -> None:
        compiled = [(e, re.compile("|".join(f"(?:{p})" for p in e.patterns), re.I)) for e in ents.values()]
        for d in documents:
            for s in d.sections:
                sid = section_node(d.doc_id, s.section)
                for e, rx in compiled:
                    if e.defined_in != sid and (m := rx.search(s.text)):
                        g.add_edge(sid, e.node_id, "mentions", sentence_at(s.text, m.start(), m.end()))

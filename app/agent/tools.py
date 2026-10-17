"""Agent tools. Every section a tool returns is registered as a numbered source [n]; the agent refers to
sources by number (follow_links(source=3)) and cites the same numbers in its answer."""
import logging
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from pydantic import BaseModel, Field, ValidationError

from app.chunking import Chunk
from app.graph.model import Graph, section_node
from app.graph.retriever import _load_sections, _neighbors
from app.graph.store import load_graph
from app.retrieval import hybrid_search

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 350
READ_CHARS = 2500


@dataclass
class Source:
    n: int
    chunk: Chunk
    found_by: str


@dataclass
class AgentContext:
    conn: psycopg.Connection
    tenant_id: str
    question: str
    doc_ids: list[str] | None = None      # UI selection / metadata filter: tools never search outside it
    sources: list[Source] = field(default_factory=list)
    _graph: Graph | None = None

    @property
    def graph(self) -> Graph:
        if self._graph is None:
            self._graph = load_graph(self.conn, self.tenant_id)
        return self._graph

    def register(self, chunk: Chunk, found_by: str) -> tuple[Source, bool]:
        """Numbered source for a chunk; returns (source, is_new). Sections dedupe by (doc, section)."""
        key = (chunk.doc_id, chunk.section or chunk.chunk_id)
        for s in self.sources:
            if (s.chunk.doc_id, s.chunk.section or s.chunk.chunk_id) == key:
                return s, False
        s = Source(len(self.sources) + 1, chunk, found_by)
        self.sources.append(s)
        return s, True

    def source(self, n: int) -> Source:
        if not 1 <= n <= len(self.sources):
            raise ValueError(f"there is no source [{n}]; observed sources are [1]..[{len(self.sources)}]")
        return self.sources[n - 1]

    def allowed(self, doc_id: str | None) -> bool:
        return self.doc_ids is None or doc_id in self.doc_ids


def _show(s: Source, is_new: bool, why: str | None = None, chars: int = SNIPPET_CHARS) -> str:
    head = f"[{s.n}] {s.chunk.context}" + (f"  ({why})" if why else "")
    if not is_new:
        return head + "  (already observed)"
    text = " ".join(s.chunk.text.split())
    more = f" … (cut; read_section({s.n}) for the rest)" if len(text) > chars else ""
    return f"{head}\n{text[:chars]}{more}"


HINT_TYPES = {"role": "held by a named person", "team": "led by a named person", "service": "owned by a team"}
MAX_HINTS = 4


def graph_hints(ctx: AgentContext, sources: list[Source]) -> str:
    """Point the agent at resolvable entities in what it just read ("the Data Platform lead" -> a name),
    without giving the answer away: it still has to call the tool, which also registers a citable source."""
    g = ctx.graph
    if not g.nodes:
        return ""
    hints, seen = [], set()
    for s in sources:
        node = section_node(s.chunk.doc_id, s.chunk.section) if s.chunk.section else None
        for m in g.out(node, "mentions") if node in g.nodes else []:
            e = g.nodes[m.dst]
            if e.entity_type in HINT_TYPES and e.node_id not in seen and g.out(e.node_id, "held_by", "led_by", "owned_by"):
                seen.add(e.node_id)
                hints.append(f'[{s.n}] mentions {e.entity_type} "{e.label}" ({HINT_TYPES[e.entity_type]}): '
                             f'lookup_entity("{e.label}")')
    return ("\n\nResolvable in the context graph:\n" + "\n".join(hints[:MAX_HINTS])) if hints else ""


# --- tools -------------------------------------------------------------------------

class HybridSearchArgs(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(4, ge=1, le=8)


def hybrid_search_tool(ctx: AgentContext, args: HybridSearchArgs) -> str:
    hits = hybrid_search(ctx.conn, ctx.tenant_id, args.query, top_n=args.top_k, doc_ids=ctx.doc_ids)
    if not hits:
        return "No results."
    registered = [ctx.register(c, f"hybrid_search({args.query!r})") for c, _ in hits]
    return "\n\n".join(_show(s, new) for s, new in registered) + graph_hints(ctx, [s for s, _ in registered])


class SourceArgs(BaseModel):
    source: int = Field(ge=1)


def follow_links_tool(ctx: AgentContext, args: SourceArgs) -> str:
    src = ctx.source(args.source)
    if not src.chunk.section:
        return f"[{src.n}] is a page of a PDF; it has no links in the context graph. Use hybrid_search instead."
    node = section_node(src.chunk.doc_id, src.chunk.section)
    g = ctx.graph
    if node not in g.nodes:
        return f"[{src.n}] is not in the context graph."
    targets, doc_targets = {}, {}
    for target, _, reason in _neighbors(g, node, entities=True):
        t = g.nodes[target]
        if not ctx.allowed(t.doc_id):
            continue
        if t.kind == "document":
            doc_targets.setdefault(t.doc_id, reason)
        else:
            targets.setdefault((t.doc_id, t.section), reason)
    for doc_id, reason in doc_targets.items():   # "see the Compensation Policy": its best section for the question
        hit = next(iter(hybrid_search(ctx.conn, ctx.tenant_id, ctx.question, top_n=1, doc_ids=[doc_id])), None)
        if hit and hit[0].section:
            targets.setdefault((doc_id, hit[0].section), reason)
    if not targets:
        return f"[{src.n}] has no links to other sections."
    chunks = _load_sections(ctx.conn, ctx.tenant_id, list(targets)[:6])
    registered = [(ctx.register(chunks[k], f"follow_links([{src.n}]): {why}"), why)
                  for k, why in list(targets.items())[:6] if k in chunks]
    shown = [_show(s, new, why=why) for (s, new), why in registered]
    return f"Links from [{src.n}]:\n\n" + "\n\n".join(shown) + graph_hints(ctx, [s for (s, _), _ in registered])


class EntityArgs(BaseModel):
    name: str = Field(min_length=2)


def lookup_entity_tool(ctx: AgentContext, args: EntityArgs) -> str:
    g, q = ctx.graph, args.name.strip().lower()
    ents = [n for n in g.nodes.values() if n.kind == "entity"]
    matches = [n for n in ents if n.label.lower() == q] or \
              [n for n in ents if q in n.label.lower() or n.label.lower() in q]
    if not matches:
        known = sorted({n.entity_type for n in ents})
        return f"No entity named {args.name!r}. Known entity types: {', '.join(known)}. Try hybrid_search."
    lines = []
    for e in matches[:3]:
        out = [f"{x.kind.replace('_', ' ')} {g.nodes[x.dst].label}" for x in g.out(e.node_id, "held_by", "owned_by", "led_by")]
        inc = [f"{g.nodes[x.src].label} {x.kind.replace('_', ' ')} it" for x in g.edges.values()
               if x.dst == e.node_id and x.kind in ("held_by", "owned_by", "led_by")]
        lines.append(f"{e.label} ({e.entity_type})" + (": " + "; ".join(out + inc) if out or inc else ""))
        for d in g.out(e.node_id, "defined_in"):
            t = g.nodes[d.dst]
            if ctx.allowed(t.doc_id):
                chunk = _load_sections(ctx.conn, ctx.tenant_id, [(t.doc_id, t.section)]).get((t.doc_id, t.section))
                if chunk:
                    lines.append(_show(*ctx.register(chunk, f"lookup_entity({args.name!r}): defines {e.label}"),
                                       why=f"defines {e.label}"))
        mentions = [g.nodes[x.src].label for x in g.edges.values() if x.dst == e.node_id and x.kind == "mentions"]
        if mentions:
            lines.append(f"Mentioned in: {'; '.join(mentions[:5])}" + (" …" if len(mentions) > 5 else ""))
    return "\n".join(lines)


def read_section_tool(ctx: AgentContext, args: SourceArgs) -> str:
    s = ctx.source(args.source)
    text = s.chunk.text
    return f"[{s.n}] {s.chunk.context}\n{text[:READ_CHARS]}" + (" …" if len(text) > READ_CHARS else "")


@dataclass
class Tool:
    name: str
    signature: str
    description: str
    args_model: type[BaseModel]
    run: Callable[[AgentContext, BaseModel], str]
    needs_graph: bool = False

    def call(self, ctx: AgentContext, raw_args: dict) -> str:
        try:
            args = self.args_model(**(raw_args or {}))
        except (ValidationError, TypeError) as e:
            raise ValueError(f"bad arguments for {self.name}{self.signature}: {e}") from e
        return self.run(ctx, args)


TOOLS = [
    Tool("hybrid_search", "(query, top_k=4)",
         "keyword + semantic search over the documents; returns numbered sources. Use short, focused queries.",
         HybridSearchArgs, hybrid_search_tool),
    Tool("follow_links", "(source)",
         "sections that source [n] references (\"see X §N\") or that define the terms, services, teams, roles "
         "and people it mentions. Use it to chase references and to resolve e.g. \"the Data Platform lead\".",
         SourceArgs, follow_links_tool, needs_graph=True),
    Tool("lookup_entity", "(name)",
         "what a named service, team, person, role, severity or tier is, who holds/owns/leads it, and the "
         "section that defines it.", EntityArgs, lookup_entity_tool, needs_graph=True),
    Tool("read_section", "(source)", "the full text of source [n] when its snippet was cut off.",
         SourceArgs, read_section_tool),
]

from dataclasses import dataclass, field
from typing import Literal, Optional

NodeKind = Literal["document", "section", "entity"]
# part_of: section -> parent section / document;  references: section -> section/document
# mentions: section -> entity;  defined_in: entity -> section;  owned_by / led_by / held_by: entity -> entity
EdgeKind = Literal["part_of", "references", "mentions", "defined_in", "owned_by", "led_by", "held_by"]


def doc_node(doc_id: str) -> str:
    return f"doc:{doc_id}"


def section_node(doc_id: str, section: str) -> str:
    return f"sec:{doc_id}#{section}"


def entity_node(entity_type: str, slug: str) -> str:
    return f"ent:{entity_type}:{slug}"


@dataclass
class Node:
    node_id: str
    kind: NodeKind
    label: str
    doc_id: Optional[str] = None
    section: Optional[str] = None
    entity_type: Optional[str] = None


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    evidence: Optional[str] = None


@dataclass
class SourceSection:
    section: str
    heading: str
    text: str


@dataclass
class SourceDocument:
    """What an adapter consumes: one document's sections."""
    doc_id: str
    title: str
    sections: list[SourceSection]


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    _out: dict[str, list[Edge]] = field(default_factory=dict, repr=False)

    def add_node(self, node: Node) -> Node:
        return self.nodes.setdefault(node.node_id, node)

    def add_edge(self, src: str, dst: str, kind: EdgeKind, evidence: str | None = None) -> None:
        """Self-loops and edges to unknown nodes are dropped; the first evidence for an edge wins."""
        key = (src, dst, kind)
        if src != dst and src in self.nodes and dst in self.nodes and key not in self.edges:
            self.edges[key] = edge = Edge(src, dst, kind, evidence)
            self._out.setdefault(src, []).append(edge)

    def out(self, src: str, *kinds: str) -> list[Edge]:
        return [e for e in self._out.get(src, []) if not kinds or e.kind in kinds]

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for n in self.nodes.values():
            counts[f"nodes.{n.kind}"] = counts.get(f"nodes.{n.kind}", 0) + 1
        for e in self.edges.values():
            counts[f"edges.{e.kind}"] = counts.get(f"edges.{e.kind}", 0) + 1
        return dict(sorted(counts.items()))

import logging
import re
from typing import Optional

import psycopg
from pydantic import BaseModel

from typing import Literal

from app.chunking import Chunk
from app.documents import find_documents
from app.graph.retriever import graph_search
from app.llm import chat
from app.retrieval import hybrid_search

Strategy = Literal["auto", "hybrid", "graph", "agent"]

logger = logging.getLogger(__name__)

TOP_N = 8
NOT_FOUND = "I couldn't find this in the provided documents."

SYSTEM_PROMPT = f"""You answer questions about the provided documents (company filings or internal policies)
using ONLY the numbered sources provided.
Rules:
- Cite every factual claim with its source number in square brackets, e.g. [1] or [2][3].
- Quote figures exactly as written in the source, including units and the period they refer to.
- Each source starts with a header naming its filing and period, e.g. "Form 10-Q, Q2 2022 (quarter ended
  June 30, 2022)": figures for "three months ended June 30, 2022" in that filing are Q2 2022 figures.
- If the sources cover only part of the question, answer that part and say briefly what is missing.
- Only if the sources contain nothing relevant, reply exactly: "{NOT_FOUND}"
- Be concise: a direct answer first, then brief supporting detail.
- When comparing several figures (across periods, segments, regions or companies), use a markdown table,
  with the source citation in each row's last column. Otherwise answer in plain prose.
- Some sources say why they were included, e.g. "(linked: referenced from Incident Response Runbook §3.1)".
  Use these links to chain facts across documents (e.g. service -> tier -> severity -> escalation) and cite
  each step of the chain."""


class Citation(BaseModel):
    source: int           # the [n] used in the answer
    doc_id: str
    page: Optional[int] = None     # PDFs
    section: Optional[str] = None  # markdown
    chunk_id: str
    context: str
    snippet: str          # passage around the first cited figure found in the chunk
    highlights: list[str] = []  # figures from the answer that appear in this chunk
    text: str = ""              # full chunk text
    via: Optional[str] = None   # graph strategy: why this source was pulled in


class Answer(BaseModel):
    answer: str
    citations: list[Citation]
    strategy: str = "hybrid"    # the strategy actually used
    steps: list[dict] = []      # agent strategy: the think / tool / observe trace


FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
YEAR_RE = re.compile(r"(?:19|20)\d\d")
SNIPPET_RADIUS = 250


def _sources_block(chunks: list[Chunk], vias: list[str | None]) -> str:
    return "\n\n".join(f"[{i}] {c.context}" + (f" (linked: {via})" if via else "") + f"\n{c.text}"
                       for i, (c, via) in enumerate(zip(chunks, vias), start=1))


def _has_graph(conn: psycopg.Connection, tenant_id: str) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM graph_nodes WHERE tenant_id = %s)", (tenant_id,)).fetchone()[0]


def _figures_cited_by(answer: str, n: int) -> list[str]:
    """Figures in the answer sentences backed by [n]; years and single digits are too generic to locate.

    A sentence without its own citation is backed by the next cited sentence: LLMs often cite once
    at the end of a passage ("Revenue was $17,455M. That's up 57% from $11,139M [1].").
    Lines are units too, so each markdown table row is backed by its own citation.
    """
    backed, carried = [], set()
    for s in reversed(re.split(r"(?<=[.!?])\s+|\n", answer)):
        carried = set(re.findall(r"\[(\d+)\]", s)) or carried
        if str(n) in carried:
            backed.append(s)
    figures = {f.rstrip(".,") for s in backed for f in FIGURE_RE.findall(re.sub(r"\[\d+\]", "", s))}
    return sorted((f for f in figures if len(f) > 1 and not YEAR_RE.fullmatch(f)), key=lambda f: (-len(f), f))


def _snippet(text: str, figures: list[str]) -> tuple[str, list[str]]:
    """Window of text around the first figure found; falls back to the chunk start."""
    found = [f for f in figures if f in text]
    if not found:
        return text[: 2 * SNIPPET_RADIUS].strip() + ("…" if len(text) > 2 * SNIPPET_RADIUS else ""), []
    pos = min(text.index(f) for f in found)
    start, end = max(0, pos - SNIPPET_RADIUS), min(len(text), pos + SNIPPET_RADIUS)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else ""), found


def answer_question(
    conn: psycopg.Connection,
    tenant_id: str,
    question: str,
    ticker: str | None = None,
    fiscal_year: int | None = None,
    doc_ids: list[str] | None = None,
    strategy: Strategy = "auto",
    on_step=None,
) -> Answer:
    """Metadata filter -> retrieval -> cited answer from the local LLM.

    doc_ids (explicit selection) and ticker/fiscal_year combine as an intersection.
    strategy: "hybrid" (BM25 + vectors), "graph" (hybrid seeds + context-graph expansion for multi-hop),
    "auto" = graph when the tenant has a context graph, else hybrid.
    "agent" = a ReAct agent that calls hybrid_search / graph tools itself (app.agent.react).
    """
    if ticker or fiscal_year:
        matched = [d.doc_id for d in find_documents(conn, tenant_id, ticker=ticker, fiscal_year=fiscal_year)]
        doc_ids = matched if doc_ids is None else [d for d in matched if d in set(doc_ids)]

    if strategy == "agent":
        from app.agent.react import run_agent   # local import: the agent itself builds on this module
        return run_agent(conn, tenant_id, question, doc_ids=doc_ids, on_step=on_step)[0]
    if strategy == "auto":
        strategy = "graph" if _has_graph(conn, tenant_id) else "hybrid"
    if strategy == "graph":
        hits = graph_search(conn, tenant_id, question, top_n=TOP_N, doc_ids=doc_ids)
        chunks, vias = [h.chunk for h in hits], [h.via for h in hits]
    else:
        chunks = [c for c, _ in hybrid_search(conn, tenant_id, question, top_n=TOP_N, doc_ids=doc_ids)]
        vias = [None] * len(chunks)
    logger.info("answering with strategy=%s over %d sources", strategy, len(chunks))
    if not chunks:
        return Answer(answer=NOT_FOUND, citations=[], strategy=strategy)

    text = chat(SYSTEM_PROMPT, f"Sources:\n\n{_sources_block(chunks, vias)}\n\nQuestion: {question}")
    return Answer(answer=text, citations=build_citations(text, chunks, vias), strategy=strategy)


def build_citations(text: str, chunks: list[Chunk], vias: list[str | None]) -> list[Citation]:
    """Citations for every valid [n] in the answer; n indexes chunks (1-based)."""
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 1 <= int(n) <= len(chunks)})
    if not cited and text != NOT_FOUND:
        logger.warning("answer has no valid citations: %r", text[:200])
    citations = []
    for n in cited:
        c = chunks[n - 1]
        snippet, highlights = _snippet(c.text, _figures_cited_by(text, n))
        citations.append(Citation(source=n, doc_id=c.doc_id, page=c.page, section=c.section, chunk_id=c.chunk_id,
                                  context=c.context, snippet=snippet, highlights=highlights, text=c.text,
                                  via=vias[n - 1]))
    return citations

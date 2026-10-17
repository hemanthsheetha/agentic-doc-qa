"""Eval metrics. Each is a small pure function so new ones can be added alongside."""
import json
import logging
import os
import re

from app.chunking import Chunk
from app.generate import NOT_FOUND, Citation
from app.llm import chat

logger = logging.getLogger(__name__)

JUDGE_MODEL = os.getenv("JUDGE_MODEL")   # None = same local LLM that answers (weak but free; swap in a stronger one)
VERDICT_SCORES = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0}


def norm(text: str) -> str:
    """Whitespace-insensitive, case-insensitive: '$ 17,455' (PDF layout) == '$17,455'."""
    return re.sub(r"\s+", "", text).lower()


def matches(evidence: dict, doc_id: str, text: str) -> bool:
    """`contains` is one string or a list of alternatives: filings state a figure several ways
    ("$3.2 billion" in MD&A, "3,208,323" in thousands in the statements), and any of them supports it."""
    alts = evidence["contains"] if isinstance(evidence["contains"], list) else [evidence["contains"]]
    return doc_id == evidence["doc_id"] and any(norm(alt) in norm(text) for alt in alts)


# --- retrieval -------------------------------------------------------------------

def recall_at_k(chunks: list[Chunk], evidence: list[dict], k: int) -> float:
    """Share of evidence items found in the top-k chunks."""
    top = chunks[:k]
    found = sum(any(matches(ev, c.doc_id, c.text) for c in top) for ev in evidence)
    return found / len(evidence)


def reciprocal_rank(chunks: list[Chunk], evidence: list[dict]) -> float:
    """1/rank of the first chunk that supports any evidence item; 0 if none does."""
    for rank, c in enumerate(chunks, start=1):
        if any(matches(ev, c.doc_id, c.text) for ev in evidence):
            return 1.0 / rank
    return 0.0


# --- generation ------------------------------------------------------------------

def is_not_found(answer: str) -> bool:
    return norm(NOT_FOUND) in norm(answer)


def fact_score(answer: str, facts: list[list[str]], expect_not_found: bool = False) -> float:
    """Share of key facts present in the answer; each fact lists acceptable spellings.

    For questions that should be refused, 1.0 iff the answer says it couldn't find it.
    """
    if expect_not_found:
        return float(is_not_found(answer))
    a = norm(answer)
    return sum(any(norm(alt) in a for alt in alts) for alts in facts) / len(facts)


def grounded(citations: list[Citation], evidence: list[dict]) -> bool:
    """At least one cited chunk is an evidence chunk."""
    return any(matches(ev, c.doc_id, c.text) for c in citations for ev in evidence)


JUDGE_PROMPT = """You grade answers to questions about company financial filings.
Compare the CANDIDATE answer with the REFERENCE answer. Judge factual agreement only, not style or length.
Extra details that are not in the reference are fine, unless they contradict it; do not mark an answer
down for including them.
- "correct": all key facts and figures in the reference are present and right (rounding like $17,455 million
  vs $17.5 billion is fine); no contradicting claims.
- "partially_correct": some key facts right, others missing or wrong.
- "incorrect": key facts wrong or missing, or it refuses when the reference has an answer.
If the reference says the information is unavailable, a candidate that says it cannot find it is "correct".
Respond with JSON only: {"verdict": "correct" | "partially_correct" | "incorrect", "reason": "<one sentence>"}"""


def judge(question: str, reference: str, answer: str) -> dict:
    """LLM-as-a-judge. Returns {"verdict", "score", "reason"}; unparseable output counts as incorrect."""
    raw = chat(JUDGE_PROMPT, f"QUESTION: {question}\nREFERENCE: {reference}\nCANDIDATE: {answer}",
               temperature=0.0, json_mode=True, model=JUDGE_MODEL)
    try:
        out = json.loads(raw)
        verdict = str(out.get("verdict", "")).strip().lower().replace(" ", "_")
        if verdict not in VERDICT_SCORES:
            raise ValueError(f"unknown verdict {verdict!r}")
        return {"verdict": verdict, "score": VERDICT_SCORES[verdict], "reason": str(out.get("reason", ""))}
    except (ValueError, AttributeError) as e:
        logger.warning("judge output unusable (%s): %r", e, raw[:200])
        return {"verdict": "unparseable", "score": 0.0, "reason": raw[:200]}

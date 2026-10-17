import json
from pathlib import Path

import pytest

from app.chunking import Chunk
from app.generate import NOT_FOUND, Citation
from evals import metrics

EV = [{"doc_id": "uber_2021", "contains": "17,455"}, {"doc_id": "lyft_2021", "contains": "3,208"}]


def chunk(doc_id, text):
    return Chunk(chunk_id=f"{doc_id}:p1:c0", doc_id=doc_id, page=1, text=text)


CHUNKS = [chunk("uber_2021", "noise"), chunk("uber_2021", "Revenue $ 11,139 $17,455"),
          chunk("lyft_2021", "Revenue$ 3,208.3"), chunk("uber_2021", "17,455 again")]


def test_recall_at_k_counts_evidence_items_found():
    assert metrics.recall_at_k(CHUNKS, EV, 1) == 0
    assert metrics.recall_at_k(CHUNKS, EV, 2) == 0.5
    assert metrics.recall_at_k(CHUNKS, EV, 3) == 1


def test_evidence_must_come_from_the_right_document():
    assert metrics.recall_at_k([chunk("lyft_2021", "17,455")], EV[:1], 1) == 0


def test_matching_ignores_pdf_spacing():
    assert metrics.matches({"doc_id": "d", "contains": "$17,455"}, "d", "Revenue $ 17,455")


def test_evidence_accepts_alternative_wordings():
    ev = {"doc_id": "lyft_2021", "contains": ["3,208", "Total revenue was $3.2 billion"]}
    assert metrics.matches(ev, "lyft_2021", "•Total revenue was $3.2 billion, an increase of 36%")
    assert metrics.matches(ev, "lyft_2021", "Revenue$ 3,208,323 $2,364,681")
    assert not metrics.matches(ev, "lyft_2021", "Revenue grew")


def test_reciprocal_rank():
    assert metrics.reciprocal_rank(CHUNKS, EV) == 0.5
    assert metrics.reciprocal_rank(CHUNKS[:1], EV) == 0


def test_fact_score_accepts_alternative_spellings():
    facts = [["17,455", "17.5 billion"], ["57%"]]
    assert metrics.fact_score("Revenue was $17.5 billion, up 57 %.", facts) == 1
    assert metrics.fact_score("Revenue was $17,455 million.", facts) == 0.5


def test_fact_score_for_refusals():
    assert metrics.fact_score(NOT_FOUND, [], expect_not_found=True) == 1
    assert metrics.fact_score("It was $5 million.", [], expect_not_found=True) == 0


def test_grounded():
    cite = Citation(source=1, doc_id="uber_2021", page=57, chunk_id="x", context="", snippet="", text="$ 17,455")
    assert metrics.grounded([cite], EV)
    assert not metrics.grounded([cite.model_copy(update={"text": "other"})], EV)


def test_judge_parses_and_scores(monkeypatch):
    monkeypatch.setattr(metrics, "chat", lambda *a, **k: '{"verdict": "Partially Correct", "reason": "missing 2020"}')
    assert metrics.judge("q", "ref", "ans") == {"verdict": "partially_correct", "score": 0.5, "reason": "missing 2020"}
    monkeypatch.setattr(metrics, "chat", lambda *a, **k: "not json")
    assert metrics.judge("q", "ref", "ans")["verdict"] == "unparseable"


@pytest.mark.parametrize("path", sorted((Path(__file__).resolve().parent.parent / "evals").glob("golden*.jsonl")),
                         ids=lambda p: p.name)
def test_golden_set_is_well_formed(path):
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len({i["id"] for i in items}) == len(items) >= 10
    for i in items:
        assert {"id", "question", "filters", "reference", "facts", "evidence"} <= i.keys(), i["id"]
        assert i.get("expect_not_found") or (i["facts"] and i["evidence"]), i["id"]
        assert all(isinstance(alts, list) and alts for alts in i["facts"]), i["id"]

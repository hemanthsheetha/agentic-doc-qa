import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import llm
from app.chunking import Chunk
from app.documents import Document, detect_metadata, load_documents

TEN_Q = dict(company="Uber Technologies, Inc.", ticker="UBER", form="10-Q",
             fiscal_year=2022, fiscal_quarter=1, period_end="2022-03-31")
TEN_K = dict(company="Lyft, Inc.", ticker="LYFT", form="10-K", fiscal_year=2021, period_end="2021-12-31")


def test_context_for_10q():
    doc = Document(doc_id="uber_10q_march_2022", **TEN_Q)
    assert doc.context(39) == "Uber Technologies, Inc. (UBER) Form 10-Q, Q1 2022 (quarter ended March 31, 2022), page 39"


def test_context_for_10k():
    doc = Document(doc_id="lyft_2021", **TEN_K)
    assert doc.context(12) == "Lyft, Inc. (LYFT) Form 10-K, fiscal year 2021 (year ended December 31, 2021), page 12"


@pytest.mark.parametrize("bad", [{**TEN_Q, "fiscal_quarter": None}, {**TEN_K, "fiscal_quarter": 4}, {**TEN_Q, "fiscal_quarter": 5}])
def test_quarter_must_match_form(bad):
    with pytest.raises(ValidationError):
        Document(doc_id="x", **bad)


def test_load_documents(tmp_path):
    path = tmp_path / "documents.json"
    path.write_text(json.dumps({"lyft_2021": TEN_K}))
    assert load_documents(path)["lyft_2021"].ticker == "LYFT"


def test_repo_manifest_covers_every_pdf():
    root = Path(__file__).resolve().parent.parent
    docs = load_documents()
    assert {p.stem for p in (root / "data" / "pdfs").glob("*.pdf")} <= docs.keys()


@pytest.mark.parametrize("pdf", sorted((Path(__file__).resolve().parent.parent / "data" / "pdfs").glob("*.pdf")),
                         ids=lambda p: p.stem)
def test_detect_metadata_matches_manifest(pdf):
    expected = load_documents()[pdf.stem].model_dump(exclude={"doc_id"})
    assert detect_metadata(pdf).model_dump() == expected


def test_detect_metadata_falls_back_to_file_name(tmp_path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)   # no cover page text at all
    path = tmp_path / "acme_10q_2023.pdf"
    with open(path, "wb") as f:
        writer.write(f)
    meta = detect_metadata(path)
    assert (meta.ticker, meta.form, meta.fiscal_year) == ("ACME", "10-Q", 2023)
    assert meta.company == "Acme 10Q 2023" and meta.fiscal_quarter == 4   # defaults to Dec 31 -> Q4


def test_embed_text_puts_context_first():
    c = Chunk(chunk_id="a:p1:c0", doc_id="a", page=1, text="Revenue was $17.5 billion.", context="Uber 10-K, page 1")
    assert c.embed_text == "Uber 10-K, page 1\n\nRevenue was $17.5 billion."
    assert Chunk(chunk_id="a:p1:c0", doc_id="a", page=1, text="x").embed_text == "x"


def test_unload_all_tolerates_ollama_down(monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_URL", "http://127.0.0.1:9")  # nothing listens here
    llm.unload_all()  # must not raise

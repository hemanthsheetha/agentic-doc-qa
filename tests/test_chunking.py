import io

import pytest
from pydantic import ValidationError

from app import chunking
from app.chunking import (
    DEFAULT_SEPERATORS,
    Chunk,
    ChunkerConfig,
    _chunk,
    chunk_pdf,
    read_pdf,
    recursive_character_splitting_chunker,
    sliding_window_chunker,
)

SLIDING = ChunkerConfig(strategy="sliding_window", max_chars=100, overlap_ratio=0.2)
RECURSIVE = ChunkerConfig(strategy="recursive", max_chars=1600, overlap_ratio=0.2)


def sentences(n: int) -> str:
    return ". ".join(f"Sentence number {i:04d} is here" for i in range(n)) + "."


# --- ChunkerConfig ---------------------------------------------------------

def test_recursive_config_gets_default_separators_copy():
    cfg = ChunkerConfig(strategy="recursive")
    assert cfg.separators == DEFAULT_SEPERATORS
    assert cfg.separators is not DEFAULT_SEPERATORS


def test_sliding_window_rejects_separators():
    with pytest.raises(ValidationError):
        ChunkerConfig(strategy="sliding_window", separators=["\n"])


@pytest.mark.parametrize("kwargs", [{"max_chars": 99}, {"overlap_ratio": 1.0}, {"overlap_ratio": -0.1}])
def test_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValidationError):
        ChunkerConfig(**kwargs)


# --- sliding window --------------------------------------------------------

@pytest.mark.parametrize("length", [1, 99, 100, 200, 300, 1234])
def test_sliding_window_covers_text_with_exact_overlap(length):
    text = "".join(chr(97 + i % 26) for i in range(length))
    chunks = sliding_window_chunker(text, SLIDING)
    overlap = int(SLIDING.max_chars * SLIDING.overlap_ratio)

    assert all(len(c) <= SLIDING.max_chars for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev[-overlap:] == nxt[:overlap]
    assert chunks[0] + "".join(c[overlap:] for c in chunks[1:]) == text


def test_sliding_window_empty_text():
    assert sliding_window_chunker("", SLIDING) == []


# --- recursive -------------------------------------------------------------

def test_recursive_short_text_is_single_stripped_chunk_with_line_breaks():
    assert recursive_character_splitting_chunker("  hello\n\nworld  ", RECURSIVE) == ["hello\n\nworld"]


def test_recursive_chunks_are_verbatim_excerpts():
    """Separators are kept, so every chunk is an exact substring of the source (tables keep their rows)."""
    rows = "\n".join(f"Revenue line {i:03d} $ {i * 1000:,} $ {i * 1100:,}" for i in range(200))
    text = f"Note 2 – Revenue\n\n{rows}\n\nEnd of table. More text follows here."
    chunks = recursive_character_splitting_chunker(text, RECURSIVE)
    assert len(chunks) > 1
    assert all(c in text for c in chunks)
    assert all("\n" in c for c in chunks)


def test_recursive_empty_text():
    assert recursive_character_splitting_chunker("", RECURSIVE) == []


def test_recursive_respects_max_chars_and_keeps_all_words():
    text = sentences(300)
    chunks = recursive_character_splitting_chunker(text, RECURSIVE)

    assert len(chunks) > 1
    assert all(len(c) <= RECURSIVE.max_chars for c in chunks)
    chunk_words = {w for c in chunks for w in c.split()}
    assert set(text.split()) <= chunk_words


def test_recursive_consecutive_chunks_overlap():
    chunks = recursive_character_splitting_chunker(sentences(300), RECURSIVE)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[:30] in prev


def test_recursive_respects_custom_max_chars():
    long_sentence = "word " * 60  # ~300 chars, no '. ' inside
    paragraph = f"{long_sentence}. {long_sentence}"
    text = f"{paragraph}\n\n{paragraph}"
    cfg = ChunkerConfig(strategy="recursive", max_chars=200)
    assert all(len(c) <= 200 for c in recursive_character_splitting_chunker(text, cfg))


def test_recursive_handles_unbreakable_text():
    chunks = recursive_character_splitting_chunker("x" * 2000, RECURSIVE)
    assert all(len(c) <= RECURSIVE.max_chars for c in chunks)


# --- page / pdf ------------------------------------------------------------

def test_chunk_attaches_citation_metadata():
    assert _chunk("hello world", 3, "lyft_2021", RECURSIVE) == [
        Chunk(chunk_id="lyft_2021:p3:c0", doc_id="lyft_2021", page=3, text="hello world")
    ]


def test_chunk_ids_are_unique_per_page():
    chunks = _chunk("a" * 350, 1, "doc", SLIDING)
    ids = [c.chunk_id for c in chunks]
    assert ids == [f"doc:p1:c{i}" for i in range(len(chunks))]


def test_chunk_blank_page_yields_nothing():
    assert _chunk("   \n\n  ", 1, "doc", RECURSIVE) == []


def test_chunk_pdf_numbers_pages_from_one_and_uses_file_stem(monkeypatch, tmp_path):
    monkeypatch.setattr(chunking, "read_pdf", lambda _: ["first page", "", "third page"])
    chunks = chunk_pdf(tmp_path / "report.pdf", RECURSIVE)

    assert [(c.doc_id, c.page, c.text) for c in chunks] == [
        ("report", 1, "first page"),
        ("report", 3, "third page"),
    ]


def test_read_pdf_accepts_path_and_bytes(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    path = tmp_path / "blank.pdf"
    path.write_bytes(buf.getvalue())

    assert read_pdf(path) == ["", ""]
    assert read_pdf(buf.getvalue()) == ["", ""]

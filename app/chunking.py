import logging
import re
import time
from typing import Literal, Optional
from pathlib import Path
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

DEFAULT_SEPERATORS = ["\n\n","\n", ". ", " ", ""]

class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    page: Optional[int] = None      # PDFs
    text: str
    context: str = ""               # document header, set at ingest from Document metadata
    section: Optional[str] = None   # markdown: "3.1"
    heading: Optional[str] = None   # markdown: "SEV1 escalation"

    @property
    def location(self) -> str:
        """Human citation location: 'p.57' or '§3.1'."""
        return f"§{self.section}" if self.section else f"p.{self.page}"

    @property
    def embed_text(self) -> str:
        """What gets embedded: context first so the vector knows company/form/period."""
        return f"{self.context}\n\n{self.text}" if self.context else self.text

class ChunkerConfig(BaseModel):
    strategy: Literal["sliding_window", "recursive"] = "recursive"
    max_chars: int = Field(2000, ge=100)
    overlap_ratio: float = Field(0.2, ge=0, lt=1)
    separators: Optional[list] = None

    @model_validator(mode="after")
    def set_separators(self):
        if self.strategy == "recursive" and self.separators is None:
            self.separators = DEFAULT_SEPERATORS.copy()
        elif self.strategy == "sliding_window" and self.separators is not None:
            raise ValueError("separators only apply to recursive strategy")
        return self
 
def sliding_window_chunker(text: str, cfg: ChunkerConfig) -> list[str]:
    chunks = []    
    if cfg.max_chars <= 0 or cfg.overlap_ratio <= 0 or cfg.overlap_ratio >= 1:
        raise ValueError("max_chars and overlap must be greater than 0 and less than 1")
    step = cfg.max_chars - int(cfg.max_chars * cfg.overlap_ratio)
    for i in range(0, len(text), step):
        chunks.append(text[i:i+cfg.max_chars])
        if i + cfg.max_chars > len(text):
            break
    return chunks

def _split(text: str, cfg: ChunkerConfig) -> list[str]:
    if not text:
        return []
    primary_separator = None
    for i, separator in enumerate(cfg.separators):
        if separator in text:
            primary_separator = separator
            break

    if primary_separator is None:
        return [text]
    if primary_separator == "":                          # no natural break left: hard cut
        return [text[j:j + cfg.max_chars] for j in range(0, len(text), cfg.max_chars)]

    # Keep the separator on each piece so merged chunks keep their line breaks (tables, paragraphs).
    parts = text.split(primary_separator)
    pieces = [p + primary_separator for p in parts[:-1]] + [parts[-1]]
    out = []
    for piece in pieces:
        if len(piece) <= cfg.max_chars:
            out.append(piece)
        else:
            out.extend(_split(piece, cfg.model_copy(update={"separators": cfg.separators[i + 1:]})))
    return out


def _merge(pieces: list[str], max_chars: int, overlap_chars: int) -> list[str]:
    """Pack pieces into chunks of <= max_chars, carrying ~overlap_chars of tail into the next chunk."""
    chunks, window, size = [], [], 0
    for p in pieces:
        if window and size + len(p) > max_chars:
            chunks.append("".join(window).strip())
            while window and (size > overlap_chars or size + len(p) > max_chars):
                size -= len(window[0])
                window.pop(0)
        window.append(p)
        size += len(p)
    if window:
        chunks.append("".join(window).strip())
    return [c for c in chunks if c]


def recursive_character_splitting_chunker(text: str, cfg: ChunkerConfig) -> list[str]:
    return _merge(_split(text, cfg), cfg.max_chars, int(cfg.max_chars * cfg.overlap_ratio))

def read_pdf(path_or_bytes) -> list[str]:
    import io

    from pypdf import PdfReader
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes
    return [(p.extract_text() or "") for p in PdfReader(src).pages]

def _chunk(text: str, page: int, doc_id: str, cfg: ChunkerConfig) -> list[Chunk]:
    if cfg.strategy == "sliding_window":
        pieces = sliding_window_chunker(text, cfg)
    else:
        pieces = recursive_character_splitting_chunker(text, cfg)
    chunks = [
        Chunk(chunk_id=f"{doc_id}:p{page}:c{i}", doc_id=doc_id, page=page, text=piece)
        for i, piece in enumerate(pieces)
        if piece.strip()
    ]
    logger.debug("%s p.%d: %d chars -> %d chunks", doc_id, page, len(text), len(chunks))
    return chunks

def chunk_pdf(path: str | Path, cfg: ChunkerConfig) -> list[Chunk]:
    start = time.perf_counter()
    path = Path(path)
    doc_id = path.stem
    pages = read_pdf(path)
    empty = sum(not p.strip() for p in pages)
    if empty:
        logger.warning("%s: %d/%d pages have no extractable text (scanned?)", doc_id, empty, len(pages))
    # Sequential: splitting is cheap next to PDF parsing and embedding.
    chunks = [c for page, text in enumerate(pages, start=1) for c in _chunk(text, page, doc_id, cfg)]
    logger.info("%s: %d pages -> %d chunks in %.1fs", doc_id, len(pages), len(chunks), time.perf_counter() - start)
    return chunks


# --- markdown ------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")


class MarkdownSection(BaseModel):
    section: str        # "3.1" for "### 3.1 SEV1 escalation"; slug for unnumbered headings; "0" for the preamble
    heading: str
    level: int          # 2 for ##, 3 for ###
    text: str           # body only, without the heading line


def parse_markdown(markdown: str) -> tuple[str | None, list[MarkdownSection]]:
    """Title (first H1) and sections, one per ##+ heading. Text before the first ## is section "0"."""
    title, sections = None, []
    current = MarkdownSection(section="0", heading="Overview", level=2, text="")
    body: list[str] = []

    def flush():
        current.text = "\n".join(body).strip()
        if current.text or current.section != "0":
            sections.append(current)

    for line in markdown.splitlines():
        m = _HEADING_RE.match(line)
        if not m:
            body.append(line)
            continue
        level, heading = len(m.group(1)), m.group(2)
        if level == 1:
            title = title or heading
            continue
        flush()
        num = _NUMBERED_RE.match(heading)
        section, heading = (num.group(1), num.group(2)) if num else (re.sub(r"\W+", "-", heading.lower()).strip("-"), heading)
        current, body = MarkdownSection(section=section, heading=heading, level=level, text=""), []
    flush()
    return title, sections


def chunk_markdown(path: str | Path, cfg: ChunkerConfig) -> list[Chunk]:
    """One chunk per section (split further only if a section exceeds max_chars), so '§3.1' maps to its chunks."""
    path = Path(path)
    doc_id = path.stem
    _, sections = parse_markdown(path.read_text(encoding="utf-8"))
    chunks = [
        Chunk(chunk_id=f"{doc_id}:s{s.section}:c{i}", doc_id=doc_id, section=s.section, heading=s.heading, text=piece)
        for s in sections if s.text
        for i, piece in enumerate(recursive_character_splitting_chunker(s.text, cfg))
    ]
    logger.info("%s: %d sections -> %d chunks", doc_id, len(sections), len(chunks))
    return chunks
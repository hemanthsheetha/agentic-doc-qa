import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from psycopg.rows import dict_row
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

DOCUMENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "documents.json"


class DocumentMetadata(BaseModel):
    """Document-level metadata: drives chunk context and metadata filtering.

    kind="sec_filing" (PDF 10-K/10-Q): company, ticker, form, fiscal_year, period_end are required.
    kind="document" (e.g. a markdown policy): only title is required.
    """
    kind: Literal["sec_filing", "document"] = "sec_filing"
    title: Optional[str] = Field(None, examples=["Incident Response Runbook"])
    company: Optional[str] = Field(None, min_length=1, examples=["Uber Technologies, Inc."])
    ticker: Optional[str] = Field(None, min_length=1, max_length=10, examples=["UBER"])
    form: Optional[Literal["10-K", "10-Q"]] = None
    fiscal_year: Optional[int] = Field(None, ge=1990, le=2100)
    fiscal_quarter: Optional[int] = Field(None, ge=1, le=4, description="Required for 10-Q, omitted for 10-K")
    period_end: Optional[date] = None

    @model_validator(mode="after")
    def normalize_and_check(self):
        if self.kind == "document":
            if not self.title:
                raise ValueError("title is required for kind=document")
            return self
        missing = [f for f in ("company", "ticker", "form", "fiscal_year", "period_end") if getattr(self, f) is None]
        if missing:
            raise ValueError(f"sec_filing requires {', '.join(missing)}")
        self.ticker = self.ticker.strip().upper()
        if (self.form == "10-Q") != (self.fiscal_quarter is not None):
            raise ValueError("fiscal_quarter is required for 10-Q and not allowed for 10-K")
        return self

    @property
    def period(self) -> str:
        end = f"{self.period_end:%B} {self.period_end.day}, {self.period_end.year}"
        if self.fiscal_quarter:
            return f"Q{self.fiscal_quarter} {self.fiscal_year} (quarter ended {end})"
        return f"fiscal year {self.fiscal_year} (year ended {end})"

    def context(self, page: int | None = None, section: str | None = None, heading: str | None = None) -> str:
        """e.g. 'Uber Technologies, Inc. (UBER) Form 10-Q, Q1 2022 (quarter ended March 31, 2022), page 39'
        or   'Incident Response Runbook §3.1 SEV1 escalation'"""
        if self.kind == "document":
            return f"{self.title} §{section} {heading}".strip() if section else self.title
        return f"{self.company} ({self.ticker}) Form {self.form}, {self.period}, page {page}"


class Document(DocumentMetadata):
    doc_id: str


class IndexedDocument(Document):
    """A document plus what is actually stored for it in the index."""
    chunks: int
    pages: int                   # pages (PDF) or sections (markdown) that produced at least one chunk
    ingested_at: datetime


_FORM_RE = re.compile(r"FORM\s+(10-[KQ])\b", re.I)
_PERIOD_RE = re.compile(r"(?:fiscal\s+year|quarterly\s+period)\s+ended\s+([A-Z][a-z]+)\s+(\d{1,2})\s*,\s*(\d{4})", re.I)
_SUFFIX = r"(?:INC\.|Inc\.|CORPORATION|Corporation|CORP\.|Corp\.|COMPANY|Company|LLC|Ltd\.|PLC|plc|N\.V\.|S\.A\.)"
_REGISTRANT_RE = re.compile(r"([A-Z][A-Za-z0-9&.,'\- ]{1,80}?" + _SUFFIX + r")\s*\(\s*Exact name of registrant")
_COMPANY_RE = re.compile(r"\b([A-Z][A-Za-z0-9&.\-]+(?:[ ,]+[A-Z][A-Za-z0-9&.\-]+)*,?\s" + _SUFFIX + r")")
_TICKER_RE = re.compile(r"per share\s*([A-Z]{1,5})\s+(?:The\s+)?(?:Nasdaq|New York Stock Exchange|NYSE)")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def detect_metadata(path: str | Path) -> DocumentMetadata:
    """Best-effort filing metadata from an SEC cover page, with file-name defaults for anything not found.

    Assumes a calendar fiscal year for the quarter number.
    """
    path = Path(path)
    if path.suffix.lower() == ".md":   # generic document: the H1 is the title
        first_h1 = re.search(r"^#\s+(.+)$", path.read_text(encoding="utf-8"), re.M)
        meta = DocumentMetadata(kind="document",
                                title=first_h1.group(1).strip() if first_h1 else path.stem.replace("-", " ").title())
        logger.info("%s: detected metadata %s", path.name, meta.model_dump(mode="json", exclude_none=True))
        return meta

    from pypdf import PdfReader

    text = re.sub(r"[ \t]+", " ", " ".join((p.extract_text() or "") for p in PdfReader(path).pages[:3]))
    stem_words = re.split(r"[_\-\s]+", path.stem)

    form = (m.group(1).upper() if (m := _FORM_RE.search(text)) else
            "10-Q" if re.search(r"10-?q|q[1-4]", path.stem, re.I) else "10-K")
    if (m := _PERIOD_RE.search(text)) and m.group(1).lower() in _MONTHS:
        period_end = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    else:
        years = re.findall(r"(?:19|20)\d\d", path.stem)
        period_end = date(int(years[-1]) if years else date.today().year, 12, 31)
    name = (m.group(1) if (m := _REGISTRANT_RE.search(text)) or (m := _COMPANY_RE.search(text))
            else " ".join(stem_words).title())
    name = " ".join(name.split())
    company = name.title() if name.isupper() else name
    ticker = m.group(1) if (m := _TICKER_RE.search(text)) else stem_words[0][:5].upper()

    meta = DocumentMetadata(
        company=company, ticker=ticker, form=form, fiscal_year=period_end.year,
        fiscal_quarter=(period_end.month - 1) // 3 + 1 if form == "10-Q" else None, period_end=period_end,
    )
    logger.info("%s: detected metadata %s", path.name, meta.model_dump(mode="json"))
    return meta


def load_documents(path: str | Path = DOCUMENTS_PATH) -> dict[str, Document]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {doc_id: Document(doc_id=doc_id, **meta) for doc_id, meta in raw.items()}


FIND_DOCUMENTS_SQL = """
SELECT doc_id, kind, title, company, ticker, form, fiscal_year, fiscal_quarter, period_end
FROM documents
WHERE tenant_id = %(tenant_id)s
  AND (%(ticker)s::text IS NULL OR ticker = %(ticker)s)
  AND (%(fiscal_year)s::int IS NULL OR fiscal_year = %(fiscal_year)s)
ORDER BY ticker NULLS LAST, period_end NULLS LAST, title
"""


def find_documents(
    conn, tenant_id: str, ticker: str | None = None, fiscal_year: int | None = None
) -> list[Document]:
    """Metadata filter, run before search. No filters returns every filing of the tenant."""
    params = {"tenant_id": tenant_id, "ticker": ticker.strip().upper() if ticker else None, "fiscal_year": fiscal_year}
    with conn.cursor(row_factory=dict_row) as cur:
        docs = [Document(**row) for row in cur.execute(FIND_DOCUMENTS_SQL, params).fetchall()]
    logger.info("find_documents tenant=%s ticker=%s fiscal_year=%s -> %s",
                tenant_id, params["ticker"], fiscal_year, [d.doc_id for d in docs] or "no matches")
    return docs


INDEXED_DOCUMENTS_SQL = """
SELECT d.doc_id, d.kind, d.title, d.company, d.ticker, d.form, d.fiscal_year, d.fiscal_quarter, d.period_end,
       count(c.chunk_id)::int AS chunks,
       count(DISTINCT coalesce(c.page::text, c.section))::int AS pages,
       greatest(d.updated_at, max(c.updated_at)) AS ingested_at
FROM documents d
LEFT JOIN chunks c ON c.tenant_id = d.tenant_id AND c.doc_id = d.doc_id
WHERE d.tenant_id = %s
GROUP BY d.tenant_id, d.doc_id
ORDER BY d.ticker NULLS LAST, d.period_end NULLS LAST, d.title
"""


def indexed_documents(conn, tenant_id: str) -> list[IndexedDocument]:
    """Every filing of the tenant with its chunk/page counts and last ingest time."""
    with conn.cursor(row_factory=dict_row) as cur:
        return [IndexedDocument(**row) for row in cur.execute(INDEXED_DOCUMENTS_SQL, (tenant_id,)).fetchall()]

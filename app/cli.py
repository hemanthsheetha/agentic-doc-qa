"""Developer CLI (the API is the main entry point).

  python -m app.cli ingest [--tenant default]       ingest data/pdfs using data/documents.json
  python -m app.cli --tenant ops ingest-dir data/corpus   ingest every .pdf/.md in a folder (metadata detected)
  python -m app.cli --tenant ops build-graph         rebuild the tenant's context graph from its markdown
  python -m app.cli search "question" [--ticker UBER] [--year 2021] [--tenant default]
  python -m app.cli ask    "question" [--ticker UBER] [--year 2021] [--tenant default]
"""
import argparse
import logging
import sys
from pathlib import Path

from app.documents import Document, detect_metadata, find_documents, load_documents
from app.embeddings import release_gpu
from app.generate import answer_question
from app.graph.store import rebuild_graph
from app.logging_config import setup_logging
from app.retrieval import hybrid_search
from app.store import DEFAULT_TENANT, connect, ingest_file

logger = logging.getLogger("app.cli")
PDF_DIR = Path(__file__).resolve().parent.parent / "data" / "pdfs"


def ingest(tenant: str) -> int:
    documents = load_documents()
    failed = []
    with connect() as conn:
        pdfs = sorted(PDF_DIR.glob("*.pdf"))
        for i, path in enumerate(pdfs, start=1):
            logger.info("[%d/%d] %s", i, len(pdfs), path.name)
            doc = documents.get(path.stem)
            if doc is None:
                logger.error("%s: no metadata in data/documents.json; skipping", path.stem)
                failed.append(path.stem)
                continue
            try:
                ingest_file(conn, tenant, path, doc)
            except Exception:
                logger.exception("%s failed, continuing", path.stem)
                failed.append(path.stem)
    release_gpu()
    logger.info("ingest finished: %d ok, %d failed %s", len(pdfs) - len(failed), len(failed), failed or "")
    return 1 if failed else 0


def ingest_dir(tenant: str, folder: Path) -> int:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".pdf", ".md"))
    failed = []
    with connect() as conn:
        for i, path in enumerate(files, start=1):
            logger.info("[%d/%d] %s", i, len(files), path.name)
            try:
                ingest_file(conn, tenant, path, Document(doc_id=path.stem, **detect_metadata(path).model_dump()))
            except Exception:
                logger.exception("%s failed, continuing", path.stem)
                failed.append(path.stem)
        if any(p.suffix.lower() == ".md" for p in files):
            print("graph:", rebuild_graph(conn, tenant))
    release_gpu()
    logger.info("ingest finished: %d ok, %d failed %s", len(files) - len(failed), len(failed), failed or "")
    return 1 if failed else 0


def build_graph(tenant: str) -> int:
    with connect() as conn:
        print("graph:", rebuild_graph(conn, tenant))
    return 0


def search(tenant: str, question: str, ticker: str | None, year: int | None) -> int:
    with connect() as conn:
        doc_ids = None
        if ticker or year:
            doc_ids = [d.doc_id for d in find_documents(conn, tenant, ticker=ticker, fiscal_year=year)]
        for chunk, score in hybrid_search(conn, tenant, question, top_n=5, doc_ids=doc_ids):
            print(f"[{chunk.doc_id}, {chunk.location}] {score:.4f}  {chunk.text[:150]!r}")
    return 0


def ask(tenant: str, question: str, ticker: str | None, year: int | None, strategy: str = "auto") -> int:
    with connect() as conn:
        result = answer_question(conn, tenant, question, ticker=ticker, fiscal_year=year, strategy=strategy)
    print(f"[strategy={result.strategy}]")
    for st in result.steps:
        call = f"{st['tool']}({', '.join(f'{k}={v!r}' for k, v in st['args'].items())})" if st.get("tool") else "answer"
        print(f"  step {st['step']}: {st['thought'][:110]}")
        print(f"      -> {call}" + (f"  ERROR {st['error']}" if st.get("error") else ""))
    print(result.answer)
    for c in result.citations:
        print(f"  [{c.source}] {c.doc_id}, {f'§{c.section}' if c.section else f'p.{c.page}'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="ingest data/pdfs")
    sub.add_parser("ingest-dir", help="ingest a folder of .pdf/.md files").add_argument("folder", type=Path)
    sub.add_parser("build-graph", help="rebuild the context graph")
    for name in ("search", "ask"):
        p = sub.add_parser(name, help="hybrid search" if name == "search" else "cited answer")
        p.add_argument("question", nargs="+")
        p.add_argument("--ticker", help="e.g. UBER")
        p.add_argument("--year", type=int, help="fiscal year, e.g. 2021")
        if name == "ask":
            p.add_argument("--strategy", choices=["auto", "hybrid", "graph", "agent"], default="auto")
    args = parser.parse_args()

    setup_logging()
    if args.command == "ingest":
        return ingest(args.tenant)
    if args.command == "ingest-dir":
        return ingest_dir(args.tenant, args.folder)
    if args.command == "build-graph":
        return build_graph(args.tenant)
    if args.command == "search":
        return search(args.tenant, " ".join(args.question), args.ticker, args.year)
    return ask(args.tenant, " ".join(args.question), args.ticker, args.year, args.strategy)


if __name__ == "__main__":
    sys.exit(main())

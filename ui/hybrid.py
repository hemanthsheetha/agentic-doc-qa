"""Filings Chat: SEC 10-K/10-Q filings with hybrid search (BM25 + vectors, RRF).

Run:  streamlit run ui/hybrid.py --server.port 8501     (API: uvicorn app.api.main:app --port 8000)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so `core` imports when run by streamlit
from core import UiConfig, main  # noqa: E402

main(UiConfig(
    title="Filings Chat",
    icon="📄",
    default_tenant="default",
    strategy="hybrid",
    welcome="Ask about your filings, or attach PDFs with 📎 below. Answers cite the pages they come from.",
    suggestions=[
        "What was Uber's total revenue in 2021?",
        "Compare Uber's revenue for each quarter of 2022",
        "What are Lyft's main risk factors?",
    ],
    upload_types=["pdf"],
    thinking="Reading the filings…",
))

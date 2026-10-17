"""Policy Graph Chat: internal policies with context-graph retrieval for multi-hop questions.

Run:  streamlit run ui/graph.py --server.port 8502      (API: uvicorn app.api.main:app --port 8000)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so `core` imports when run by streamlit
from core import UiConfig, main  # noqa: E402

main(UiConfig(
    title="Policy Graph Chat",
    icon="🕸️",
    default_tenant="ops",
    strategy="graph",
    welcome="Ask questions that span several policies; answers follow references between documents "
            "and show how each source was linked. Attach markdown with 📎 below.",
    suggestions=[
        "The Route Optimizer is down. When is the first customer update due, and who is paged after 15 minutes?",
        "We lost customer data. Which specific person must be paged, and when must the regulator be told?",
        "Who must approve a Normal change to the Driver App, and how fast must its rollback complete?",
    ],
    upload_types=["md", "pdf"],
    thinking="Following references across documents…",
    graph_panel=True,
))

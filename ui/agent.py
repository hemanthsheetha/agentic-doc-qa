"""Agent Chat: a ReAct agent that thinks, picks a tool (hybrid_search, follow_links, lookup_entity,
read_section), observes, and repeats (max 8 steps) before giving a cited answer. The trace is shown live.

Run:  streamlit run ui/agent.py --server.port 8503      (API: uvicorn app.api.main:app --port 8000)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so `core` imports when run by streamlit
from core import UiConfig, main  # noqa: E402

main(UiConfig(
    title="Agent Chat",
    icon="🧠",
    default_tenant="ops",
    strategy="agent",
    welcome="An agent plans its own searches and follows links between documents, step by step. "
            "Watch its reasoning live; every answer cites what it actually read.",
    suggestions=[
        "Which person, by name, must approve a Normal change to the Driver App, and how fast must its rollback plan complete?",
        "We lost customer data. Which specific person must be paged, and when must the regulator be told?",
        "Is a 20-day-old copy of the Billing Service data still available to restore?",
    ],
    upload_types=["md", "pdf"],
    thinking="Planning…",
    graph_panel=True,
))

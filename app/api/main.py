"""HTTP API: async ingestion and question-answering jobs, scoped per tenant.

Run:  uvicorn app.api.main:app --port 8000      (docs at http://localhost:8000/docs)
"""
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import jobs
from app.api.routers import documents, graph, health, ingestion, queries
from app.embeddings import warm_up
from app.logging_config import setup_logging


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    # Warm up in the background: uvicorn binds the port only after startup finishes, so a
    # blocking warm-up delays the server (and port-in-use errors) by ~15s. Queries that
    # arrive meanwhile wait on the model load lock.
    threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
    jobs.start_pools()
    yield
    jobs.stop_pools()


app = FastAPI(title="Agentic Doc QA", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(documents.router)
app.include_router(graph.router)
app.include_router(ingestion.router)
app.include_router(queries.router)

import json
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api import jobs, main
from app.api.routers import documents, health, ingestion
from app.generate import Answer, Citation

UBER_10K = {"company": "Uber Technologies, Inc.", "ticker": "UBER", "form": "10-K",
            "fiscal_year": 2021, "period_end": "2021-12-31"}


@contextmanager
def fake_connect():
    yield object()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "warm_up", lambda: None)
    monkeypatch.setattr(jobs, "release_gpu", lambda: None)
    monkeypatch.setattr(jobs, "connect", fake_connect)
    with TestClient(main.app) as c:
        yield c


def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(health, "_check_database", lambda: "ok")
    monkeypatch.setattr(health, "_check_llm", lambda: "ok")
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "checks": {"database": "ok", "llm": "ok"}}


def test_health_degraded_returns_503(client, monkeypatch):
    def down():
        raise ConnectionError("refused")
    monkeypatch.setattr(health, "_check_database", lambda: "ok")
    monkeypatch.setattr(health, "_check_llm", down)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded" and "refused" in resp.json()["checks"]["llm"]


def wait_for(client, url, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(url).json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {body}")


def test_ingestion_job_reports_progress_and_failures(client, monkeypatch):
    def fake_ingest(conn, tenant_id, path, doc):
        assert tenant_id == "acme" and doc.doc_id == path.stem
        if doc.doc_id == "uber_10q_june_2022":
            raise ValueError("boom")
        return 3
    monkeypatch.setattr(jobs, "ingest_file", fake_ingest)

    files = [{"path": "data/pdfs/uber_2021.pdf", "metadata": UBER_10K},
             {"path": "data/pdfs/uber_10q_june_2022.pdf",
              "metadata": {**UBER_10K, "form": "10-Q", "fiscal_year": 2022, "fiscal_quarter": 2, "period_end": "2022-06-30"}}]
    resp = client.post("/tenants/acme/ingestion-jobs", json={"files": files})
    assert resp.status_code == 202
    created = resp.json()
    assert created["status_url"] == f"/tenants/acme/ingestion-jobs/{created['job_id']}"

    job = wait_for(client, created["status_url"])
    assert job["status"] == "completed_with_errors"
    assert (job["files_total"], job["files_done"], job["files_failed"], job["files_remaining"]) == (2, 1, 1, 0)
    assert job["failures"][0]["error"] == "ValueError: boom"


@pytest.mark.parametrize("files, why", [
    ([{"path": "C:/Windows/win.ini", "metadata": UBER_10K}], "outside ingest root"),
    ([{"path": "data/pdfs/missing.pdf", "metadata": UBER_10K}], "missing file"),
    ([{"path": "data/documents.json", "metadata": UBER_10K}], "not a pdf"),
    ([{"path": "data/pdfs/uber_2021.pdf", "metadata": {**UBER_10K, "form": "10-Q"}}], "10-Q without quarter"),
    ([{"path": "data/pdfs/uber_2021.pdf", "metadata": UBER_10K}] * 2, "duplicate doc_id"),
    ([], "no files"),
])
def test_ingestion_rejects_bad_requests(client, files, why):
    assert client.post("/tenants/acme/ingestion-jobs", json={"files": files}).status_code == 422, why


def test_invalid_tenant_id_rejected(client):
    assert client.post("/tenants/Bad Tenant!/query-jobs", json={"question": "q"}).status_code == 422


def test_query_job_returns_answer_with_citations(client, monkeypatch):
    def fake_answer(conn, tenant_id, question, ticker=None, fiscal_year=None, doc_ids=None, strategy="auto", on_step=None):
        assert (tenant_id, ticker, fiscal_year, doc_ids) == ("acme", "UBER", 2021, ["uber_2021"])
        return Answer(answer="Revenue was $17,455 million [1].", citations=[Citation(
            source=1, doc_id="uber_2021", page=129, chunk_id="uber_2021:p129:c0", context="ctx", snippet="Total Revenue")])
    monkeypatch.setattr(jobs, "answer_question", fake_answer)

    resp = client.post("/tenants/acme/query-jobs",
                       json={"question": "What was revenue?",
                             "filters": {"ticker": "UBER", "fiscal_year": 2021, "doc_ids": ["uber_2021"]}})
    assert resp.status_code == 202
    job = wait_for(client, resp.json()["status_url"])
    assert job["status"] == "succeeded"
    assert job["answer"].endswith("[1].") and job["citations"][0]["page"] == 129


def test_query_job_failure_is_reported(client, monkeypatch):
    def broken(*_, **__):
        raise ConnectionError("ollama down")
    monkeypatch.setattr(jobs, "answer_question", broken)
    job = wait_for(client, client.post("/tenants/acme/query-jobs", json={"question": "q"}).json()["status_url"])
    assert job["status"] == "failed" and "ollama down" in job["error"]


PDF_BYTES = b"%PDF-1.7\n fake but starts like a pdf"


@pytest.fixture
def uploads(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "UPLOAD_DIR", tmp_path)
    ingested = []
    monkeypatch.setattr(jobs, "ingest_file", lambda conn, tenant, path, doc: ingested.append((tenant, path, doc)) or 1)
    return tmp_path, ingested


def test_upload_saves_files_and_ingests_them(client, uploads):
    tmp_path, ingested = uploads
    resp = client.post("/tenants/acme/ingestion-jobs/uploads",
                       files=[("files", ("Uber 2021 (10-K).pdf", PDF_BYTES, "application/pdf"))],
                       data={"metadata": json.dumps([UBER_10K])})
    assert resp.status_code == 202, resp.text
    job = wait_for(client, resp.json()["status_url"])
    assert job["status"] == "succeeded"
    tenant, path, doc = ingested[0]
    assert tenant == "acme" and doc.doc_id == "uber_2021_10-k" and doc.ticker == "UBER"
    assert path == tmp_path / "acme" / "uber_2021_10-k.pdf" and path.read_bytes() == PDF_BYTES


def test_upload_without_metadata_detects_it(client, uploads, monkeypatch):
    from app.documents import DocumentMetadata
    _, ingested = uploads
    seen = []
    monkeypatch.setattr(jobs, "detect_metadata", lambda path: seen.append(path.name) or DocumentMetadata(**UBER_10K))
    resp = client.post("/tenants/acme/ingestion-jobs/uploads",
                       files=[("files", ("uber_2021.pdf", PDF_BYTES, "application/pdf"))])   # no metadata field
    assert resp.status_code == 202, resp.text
    assert wait_for(client, resp.json()["status_url"])["status"] == "succeeded"
    assert seen == ["uber_2021.pdf"] and ingested[0][2].ticker == "UBER"


@pytest.mark.parametrize("files, metadata, code", [
    ([("files", ("a.pdf", b"not a pdf", "application/pdf"))], [UBER_10K], 422),
    ([("files", ("a.pdf", PDF_BYTES, "application/pdf"))], [], 422),                      # count mismatch
    ([("files", ("a.pdf", PDF_BYTES, "application/pdf"))], [{**UBER_10K, "form": "10-Q"}], 422),
    ([("files", ("a.pdf", PDF_BYTES, "application/pdf")), ("files", ("A.pdf", PDF_BYTES, "application/pdf"))],
     [UBER_10K, UBER_10K], 422),                                                           # same doc_id
])
def test_upload_rejects_bad_requests(client, uploads, files, metadata, code):
    resp = client.post("/tenants/acme/ingestion-jobs/uploads", files=files, data={"metadata": json.dumps(metadata)})
    assert resp.status_code == code, resp.text
    assert not list(uploads[0].rglob("*.pdf"))   # nothing written


def test_upload_filename_cannot_escape_upload_dir(client, uploads):
    tmp_path, ingested = uploads
    resp = client.post("/tenants/acme/ingestion-jobs/uploads",
                       files=[("files", ("../../evil.pdf", PDF_BYTES, "application/pdf"))],
                       data={"metadata": json.dumps([UBER_10K])})
    wait_for(client, resp.json()["status_url"])
    assert ingested[0][1] == tmp_path / "acme" / "evil.pdf"


def test_list_documents_includes_index_stats(client, monkeypatch):
    from app.documents import IndexedDocument
    doc = IndexedDocument(doc_id="uber_2021", **UBER_10K, chunks=879, pages=307, ingested_at="2026-10-17T13:11:19Z")
    monkeypatch.setattr(documents, "connect", fake_connect)
    monkeypatch.setattr(documents, "indexed_documents", lambda conn, tenant: [doc] if tenant == "acme" else [])
    body = client.get("/tenants/acme/documents").json()
    assert [(d["doc_id"], d["chunks"], d["pages"]) for d in body] == [("uber_2021", 879, 307)]
    assert client.get("/tenants/other/documents").json() == []


def test_jobs_are_tenant_scoped(client, monkeypatch):
    monkeypatch.setattr(jobs, "answer_question", lambda *a, **k: Answer(answer="x", citations=[]))
    job_id = client.post("/tenants/acme/query-jobs", json={"question": "q"}).json()["job_id"]
    assert client.get(f"/tenants/other/query-jobs/{job_id}").status_code == 404
    assert client.get("/tenants/acme/query-jobs/nope").status_code == 404


def test_graph_stats_and_rebuild(client, monkeypatch):
    from app.api.routers import graph
    stats = {"built": True, "nodes": {"section": 73, "entity": 45}, "entities": {"team": 8}, "edges": {"references": 27}}
    rebuilt = []
    monkeypatch.setattr(graph, "connect", fake_connect)
    monkeypatch.setattr(graph, "graph_stats", lambda conn, tenant: stats if tenant == "ops" else
                        {"built": False, "nodes": {}, "entities": {}, "edges": {}})
    monkeypatch.setattr(graph, "rebuild_graph", lambda conn, tenant: rebuilt.append(tenant))
    assert client.get("/tenants/ops/graph").json() == stats
    assert client.get("/tenants/acme/graph").json()["built"] is False
    assert client.post("/tenants/ops/graph/rebuild").json()["nodes"]["section"] == 73 and rebuilt == ["ops"]

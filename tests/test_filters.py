import psycopg
import pytest

from app import retrieval
from app.documents import find_documents, indexed_documents
from app.store import DATABASE_URL, DEFAULT_TENANT


def test_empty_doc_filter_returns_nothing_without_searching(monkeypatch):
    def boom(*_):
        raise AssertionError("must not embed or query when the filter matched nothing")
    monkeypatch.setattr(retrieval, "embed", boom)
    assert retrieval.hybrid_search(conn=None, tenant_id="t", query="revenue", doc_ids=[]) == []


@pytest.fixture(scope="module")
def conn():
    try:
        c = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("postgres not reachable")
    try:
        n = c.execute("SELECT count(*) FROM documents WHERE tenant_id = %s", (DEFAULT_TENANT,)).fetchone()[0]
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        pytest.skip("current schema not applied; run db/schema.sql")
    if not n:
        pytest.skip("no documents for the default tenant; run python -m app.store")
    yield c
    c.close()


@pytest.mark.parametrize("ticker, year, expected", [
    ("UBER", 2021, {"uber_2021"}),
    ("uber", 2022, {"uber_10q_march_2022", "uber_10q_june_2022", "uber_10q_sept_2022"}),
    ("LYFT", None, {"lyft_2021"}),
    (None, 2021, {"uber_2021", "lyft_2021"}),
    ("LYFT", 2022, set()),
])
def test_find_documents(conn, ticker, year, expected):
    assert {d.doc_id for d in find_documents(conn, DEFAULT_TENANT, ticker=ticker, fiscal_year=year)} == expected


def test_find_documents_without_filters_returns_all_of_tenant(conn):
    n = conn.execute("SELECT count(*) FROM documents WHERE tenant_id = %s", (DEFAULT_TENANT,)).fetchone()[0]
    assert len(find_documents(conn, DEFAULT_TENANT)) == n


def test_indexed_documents_counts_match_chunks_table(conn):
    expected = dict(conn.execute(
        "SELECT doc_id, count(*) FROM chunks WHERE tenant_id = %s GROUP BY doc_id", (DEFAULT_TENANT,)).fetchall())
    docs = indexed_documents(conn, DEFAULT_TENANT)
    assert {d.doc_id: d.chunks for d in docs} == expected
    assert all(0 < d.pages <= d.chunks for d in docs)
    assert indexed_documents(conn, "no-such-tenant") == []


def test_find_documents_is_tenant_scoped(conn):
    assert find_documents(conn, "no-such-tenant", ticker="UBER") == []

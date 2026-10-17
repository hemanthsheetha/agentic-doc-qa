import json
import urllib.request

from fastapi import APIRouter, Response, status

from app.api.schemas import Health
from app.llm import LLM_MODEL, OLLAMA_URL
from app.store import connect

router = APIRouter(tags=["health"])


def _check_database() -> str:
    with connect() as conn:
        conn.execute("SELECT 1 FROM chunks LIMIT 1")
    return "ok"


def _check_llm() -> str:
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
        models = {m["name"] for m in json.loads(resp.read()).get("models", [])}
    return "ok" if LLM_MODEL in models else f"model {LLM_MODEL} not pulled"


@router.get("/health", response_model=Health, summary="Database and LLM reachability",
            responses={503: {"model": Health}})
def health(response: Response) -> Health:
    checks = {}
    for name, check in {"database": _check_database, "llm": _check_llm}.items():
        try:
            checks[name] = check()
        except Exception as e:
            checks[name] = f"{type(e).__name__}: {e}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Health(status="ok" if ok else "degraded", checks=checks)

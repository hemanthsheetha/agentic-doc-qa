"""Eval harness: retrieval (recall@k, MRR) and generation (key-fact match, grounding, LLM judge).

  python -m evals.run                          # everything
  python -m evals.run --retrieval-only         # fast: no LLM calls
  python -m evals.run --only q01,q08 --no-judge
  python -m evals.run --tags comparison

Golden set: evals/golden.jsonl, one JSON object per line:
  id, question, filters {ticker?, fiscal_year?}, reference (ideal answer), facts (list of acceptable
  spellings per key fact), evidence ([{doc_id, contains}]: text a supporting chunk must contain),
  tags, expect_not_found (optional).
Results are written to evals/results/<timestamp>.json, with the config they ran under, for comparing runs.
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from app.documents import find_documents
from app.embeddings import EMBED_MODEL
from app.generate import TOP_N, answer_question
from app.agent.react import run_agent
from app.graph.retriever import graph_search
from app.llm import LLM_MODEL
from app.logging_config import setup_logging
from app.retrieval import hybrid_search
from app.store import CHUNKER_CONFIG, DEFAULT_TENANT, connect
from evals import metrics

EVALS_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("evals")


def load_golden(path: Path, only: set[str] | None, tags: set[str] | None) -> list[dict]:
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in golden set")
    return [i for i in items if (not only or i["id"] in only) and (not tags or tags & set(i.get("tags", [])))]


def doc_scope(conn, tenant: str, filters: dict) -> list[str] | None:
    if not (filters.get("ticker") or filters.get("fiscal_year")):
        return None
    return [d.doc_id for d in find_documents(conn, tenant, ticker=filters.get("ticker"),
                                             fiscal_year=filters.get("fiscal_year"))]


def eval_retrieval(conn, tenant: str, item: dict, ks: list[int], strategy: str) -> dict:
    scope = doc_scope(conn, tenant, item.get("filters", {}))
    if strategy == "graph":
        chunks = [h.chunk for h in graph_search(conn, tenant, item["question"], top_n=max(ks), doc_ids=scope)]
    else:
        chunks = [c for c, _ in hybrid_search(conn, tenant, item["question"], top_n=max(ks), doc_ids=scope)]
    return {
        **{f"recall@{k}": metrics.recall_at_k(chunks, item["evidence"], k) for k in ks},
        "mrr": metrics.reciprocal_rank(chunks, item["evidence"]),
        "retrieved": [f"{c.doc_id}:{c.location}" for c in chunks],
    }


def eval_generation(conn, tenant: str, item: dict, use_judge: bool, strategy: str) -> dict:
    f = item.get("filters", {})
    start = time.perf_counter()
    result = answer_question(conn, tenant, item["question"], ticker=f.get("ticker"), fiscal_year=f.get("fiscal_year"),
                             strategy=strategy)
    out = {
        "answer": result.answer,
        "latency_s": round(time.perf_counter() - start, 1),
        "fact_score": metrics.fact_score(result.answer, item.get("facts", []), item.get("expect_not_found", False)),
        "grounded": metrics.grounded(result.citations, item["evidence"]) if item["evidence"] else None,
        "citations": [f"{c.doc_id}:" + (f"§{c.section}" if c.section else f"p.{c.page}") for c in result.citations],
    }
    if use_judge:
        out["judge"] = metrics.judge(item["question"], item["reference"], result.answer)
    return out


def eval_agent(conn, tenant: str, item: dict, ks: list[int], use_judge: bool) -> dict:
    """Retrieval is scored on the sources the agent observed, in the order it found them."""
    start = time.perf_counter()
    result, ctx = run_agent(conn, tenant, item["question"], doc_ids=doc_scope(conn, tenant, item.get("filters", {})))
    chunks = [s.chunk for s in ctx.sources]
    out = {"generation": {
        "answer": result.answer,
        "latency_s": round(time.perf_counter() - start, 1),
        "steps": len(result.steps),
        "tools": [s["tool"] for s in result.steps if s.get("tool")],
        "fact_score": metrics.fact_score(result.answer, item.get("facts", []), item.get("expect_not_found", False)),
        "grounded": metrics.grounded(result.citations, item["evidence"]) if item["evidence"] else None,
        "citations": [f"{c.doc_id}:" + (f"§{c.section}" if c.section else f"p.{c.page}") for c in result.citations],
    }}
    if use_judge:
        out["generation"]["judge"] = metrics.judge(item["question"], item["reference"], result.answer)
    if item["evidence"]:
        out["retrieval"] = {
            **{f"recall@{k}": metrics.recall_at_k(chunks, item["evidence"], k) for k in ks},
            "recall@observed": metrics.recall_at_k(chunks, item["evidence"], len(chunks) or 1),
            "mrr": metrics.reciprocal_rank(chunks, item["evidence"]),
            "retrieved": [f"{c.doc_id}:{c.location}" for c in chunks],
        }
    return out


def summarize(rows: list[dict], ks: list[int]) -> dict:
    def avg(key, src):
        vals = [r[src][key] for r in rows if src in r and r[src].get(key) is not None]
        return round(mean(vals), 3) if vals else None

    summary = {f"recall@{k}": avg(f"recall@{k}", "retrieval") for k in ks}
    summary["mrr"] = avg("mrr", "retrieval")
    summary["recall@observed"] = avg("recall@observed", "retrieval")
    summary["steps"] = avg("steps", "generation")
    summary["fact_score"] = avg("fact_score", "generation")
    summary["grounded"] = avg("grounded", "generation")
    judged = [r["generation"]["judge"]["score"] for r in rows if "judge" in r.get("generation", {})]
    summary["judge_score"] = round(mean(judged), 3) if judged else None
    summary["latency_s"] = avg("latency_s", "generation")
    return {k: v for k, v in summary.items() if v is not None}


def print_report(rows: list[dict], summary: dict, ks: list[int]) -> None:
    k_cols = "".join(f" R@{k:<3}" for k in ks)
    print(f"\n{'id':5} {'tags':26}{k_cols}  MRR   facts  grnd  judge")
    for r in rows:
        ret, gen = r.get("retrieval", {}), r.get("generation", {})
        recalls = "".join(f" {ret[f'recall@{k}']:<5.2f}" if ret else "   -  " for k in ks)
        mrr = f"{ret['mrr']:.2f}" if ret else "  - "
        facts = f"{gen['fact_score']:.2f}" if gen else "  - "
        grnd = {True: "yes", False: "NO", None: " - "}[gen.get("grounded")] if gen else " - "
        verdict = gen.get("judge", {}).get("verdict", "-") if gen else "-"
        print(f"{r['id']:5} {','.join(r['tags'])[:25]:26}{recalls}  {mrr}  {facts}   {grnd:4}  {verdict}")
    print("\nsummary:", "  ".join(f"{k}={v}" for k, v in summary.items()))
    for r in rows:   # show what went wrong, so failures are actionable
        gen = r.get("generation", {})
        if gen and (gen["fact_score"] < 1 or gen.get("judge", {}).get("score", 1) < 1):
            print(f"\n[{r['id']}] {r['question']}\n  answer: {gen['answer'][:300]}")
            if "judge" in gen:
                print(f"  judge:  {gen['judge']['verdict']}: {gen['judge']['reason']}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    p.add_argument("--golden", type=Path, default=EVALS_DIR / "golden.jsonl")
    p.add_argument("--tenant", default=DEFAULT_TENANT)
    p.add_argument("--only", help="comma-separated ids")
    p.add_argument("--tags", help="comma-separated tags (any match)")
    p.add_argument("--k", default="1,3,5,8", help="recall cut-offs")
    p.add_argument("--retrieval-only", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--strategy", choices=["hybrid", "graph", "agent"], default="hybrid",
                   help="hybrid | graph (hybrid + context graph) | agent (ReAct; retrieval = sources it observed)")
    p.add_argument("--no-filters", action="store_true",
                   help="ignore each question's ticker/year filters: search everything, like an unfiltered user")
    p.add_argument("--out", type=Path, default=EVALS_DIR / "results")
    args = p.parse_args()

    setup_logging("WARNING")
    ks = sorted({int(k) for k in args.k.split(",")})
    items = load_golden(args.golden, set(args.only.split(",")) if args.only else None,
                        set(args.tags.split(",")) if args.tags else None)
    if args.no_filters:
        items = [{**i, "filters": {}} for i in items]
    print(f"{len(items)} questions, tenant={args.tenant}, strategy={args.strategy}, embed={EMBED_MODEL}, llm={LLM_MODEL}, "
          f"judge={metrics.JUDGE_MODEL or LLM_MODEL}")

    rows = []
    with connect() as conn:
        for i, item in enumerate(items, start=1):
            print(f"[{i}/{len(items)}] {item['id']} {item['question']}", flush=True)
            row = {"id": item["id"], "tags": item.get("tags", []), "question": item["question"]}
            if args.strategy == "agent":   # one agent run gives both: observed sources and the answer
                row.update(eval_agent(conn, args.tenant, item, ks, not args.no_judge))
            else:
                if item["evidence"]:
                    row["retrieval"] = eval_retrieval(conn, args.tenant, item, ks, args.strategy)
                if not args.retrieval_only:
                    row["generation"] = eval_generation(conn, args.tenant, item, not args.no_judge, args.strategy)
            rows.append(row)

    summary = summarize(rows, ks)
    print_report(rows, summary, ks)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / f"{stamp}.json"
    out.write_text(json.dumps({
        "run_at": stamp,
        "config": {"tenant": args.tenant, "strategy": args.strategy, "embed_model": EMBED_MODEL, "llm": LLM_MODEL,
                   "judge": metrics.JUDGE_MODEL or LLM_MODEL, "top_n": TOP_N, "chunker": CHUNKER_CONFIG.model_dump(),
                   "golden": str(args.golden.name), "retrieval_only": args.retrieval_only,
                   "filters": not args.no_filters},
        "summary": summary, "questions": rows,
    }, indent=2), encoding="utf-8")
    print(f"\nsaved {out.relative_to(EVALS_DIR.parent)}")


if __name__ == "__main__":
    main()

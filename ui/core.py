"""Shared chat UI used by the hybrid and graph apps. Talks only to the HTTP API.

Each app is a thin entry point that calls main(UiConfig(...)):
  streamlit run ui/hybrid.py --server.port 8501     # filings, hybrid search
  streamlit run ui/graph.py  --server.port 8502     # policies, context graph (multi-hop)
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TERMINAL = {"succeeded", "completed_with_errors", "failed"}
CONTENT_TYPES = {".pdf": "application/pdf", ".md": "text/markdown"}

CSS = """
<style>
  /* Only the footer is hidden: the header toolbar holds the expand-sidebar arrow. */
  footer {visibility: hidden;}
  .block-container {max-width: 800px; padding-top: 2.5rem; padding-bottom: 6rem;}
  [data-testid="stChatMessage"] {background: transparent; padding: 0.75rem 0;}
  [data-testid="stSidebar"] .stCheckbox {margin-bottom: -0.6rem;}
  .welcome {text-align: center; margin: 14vh 0 2rem;}
  .welcome h2 {font-weight: 500; font-family: Georgia, serif;}
  .welcome p {opacity: 0.65;}
  [data-testid="stChatInput"] {border-radius: 1rem;}
</style>
"""


@dataclass
class UiConfig:
    title: str
    icon: str
    default_tenant: str
    strategy: str                      # "hybrid" | "graph", sent with every question
    welcome: str                       # line under "What would you like to know?"
    suggestions: list[str]
    upload_types: list[str] = field(default_factory=lambda: ["pdf", "md"])
    thinking: str = "Reading the documents…"
    graph_panel: bool = False          # sidebar panel with graph stats and a rebuild button


# --- API helpers -------------------------------------------------------------

class ApiError(Exception):
    pass


def api(method: str, path: str, **kwargs):
    try:
        resp = httpx.request(method, f"{API_URL}{path}", timeout=60, **kwargs)
    except httpx.HTTPError as e:
        raise ApiError(f"API not reachable at {API_URL}: {e}") from e
    if resp.status_code >= 400 and resp.status_code != 503:   # 503 = degraded health, still has a body
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = resp.json().get("detail", resp.text) if is_json else resp.text
        if isinstance(detail, list):  # pydantic validation errors
            detail = "; ".join(f"{'.'.join(map(str, d['loc'][1:]))}: {d['msg']}" for d in detail)
        raise ApiError(f"{resp.status_code}: {detail}")
    return resp.json()


def poll(job_ref: dict) -> dict:
    """Fetch a job's status; caches it in job_ref once finished. A vanished job (API restart) counts as failed."""
    if "result" in job_ref:
        return job_ref["result"]
    try:
        job = api("GET", job_ref["status_url"])
    except ApiError as e:
        if not str(e).startswith("404"):
            raise
        job = {**job_ref, "status": "failed", "error": "Job no longer exists (the API was probably restarted)."}
    if job["status"] in TERMINAL:
        job_ref["result"] = job
    return job


def live(fn, pending: bool) -> None:
    """Run fn as a fragment that re-runs every 2s only while something is pending."""
    st.fragment(run_every=2 if pending else None)(fn)()


def is_pending(ref: dict | None) -> bool:
    return bool(ref) and "result" not in ref


# --- rendering ---------------------------------------------------------------

MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|$~:])")


def md_escape(text: str) -> str:
    """Escape markdown, and `$` in particular: Streamlit renders $...$ as LaTeX, which mangles dollar figures."""
    return MD_SPECIAL.sub(r"\\\1", text)


def badge(n) -> str:
    return f":orange-background[**{n}**]"


def doc_label(d: dict) -> str:
    if d.get("kind") == "document":
        return d.get("title") or d["doc_id"]
    period = f"Q{d['fiscal_quarter']} {d['fiscal_year']}" if d.get("fiscal_quarter") else f"FY{d['fiscal_year']}"
    return f"{d['ticker']} · {d['form']} {period}"


def _call(s: dict) -> str:
    if not s.get("tool"):
        return "answer"
    return f"{s['tool']}({', '.join(f'{k}={v!r}' for k, v in (s.get('args') or {}).items())})"


def render_trace(steps: list[dict]) -> None:
    """Agent trace: each think -> tool -> observe step."""
    for s in steps:
        st.markdown(f"**Step {s['step']}** · `{md_escape(_call(s))}`" + (f" · {s['seconds']}s" if s.get("seconds") else ""))
        if s.get("thought"):
            st.caption(f"💭 {md_escape(s['thought'])}")
        if s.get("error"):
            st.caption(f"⚠️ {md_escape(s['error'])}")
        elif s.get("observation"):
            obs = " ".join(s["observation"].split())
            st.caption(f"👁 {md_escape(obs[:300])}" + ("…" if len(obs) > 300 else ""))


def render_answer(job: dict) -> None:
    if job["status"] == "failed":
        st.error(job.get("error") or "Something went wrong.")
        return
    if job.get("steps"):
        tools = [s["tool"] for s in job["steps"] if s.get("tool")]
        with st.expander(f"🧠 Agent trace · {len(job['steps'])} steps · " + (" → ".join(tools) or "no tools")):
            render_trace(job["steps"])
    # Answer is markdown (prose or a table): escape only `$`. [2] -> badge matching source 2 below.
    st.markdown(re.sub(r"\[(\d+)\]", lambda m: badge(m.group(1)), job["answer"].replace("$", r"\$")))
    if job.get("strategy_used") == "graph":
        linked = sum(1 for c in job.get("citations") or [] if c.get("via"))
        st.caption(f"🔗 multi-hop: context graph" + (f" · {linked} linked source{'s' * (linked != 1)} cited" if linked else ""))
    citations = job.get("citations") or []
    if not citations:
        return
    with st.expander(f"Sources · {len(citations)}"):
        for i, c in enumerate(citations):
            if i:
                st.divider()
            st.markdown(f"{badge(c['source'])} **{md_escape(c['context'])}**")
            if c.get("via"):   # graph strategy: why this source was pulled in
                st.caption(f"↳ linked: {md_escape(c['via'])}")
            snippet = md_escape(c["snippet"]).replace("\n", "  \n")   # keep line breaks (tables)
            figs = sorted((md_escape(f) for f in c.get("highlights", [])), key=len, reverse=True)
            if figs:   # one pass, longest first, so "455" never re-highlights inside "17,455"
                snippet = re.sub("|".join(map(re.escape, figs)),
                                 lambda m: f":orange-background[**{m.group(0)}**]", snippet)
            st.caption(snippet)


# --- uploads -----------------------------------------------------------------

def safe_stem(filename: str) -> str:
    """Same rule as the API, so we know the doc_ids an upload will get."""
    return re.sub(r"[^a-z0-9_-]+", "_", Path(filename).stem.lower()).strip("_") or "document"


def start_upload(tenant: str, files: list[tuple[str, bytes]], question: str | None = None) -> dict | None:
    """Upload straight away (the API detects metadata). Returns the job reference, shared by the sidebar
    and chat status cards, or None on failure."""
    names = [name for name, _ in files]
    with st.spinner(f"Uploading {', '.join(names)}…"):
        try:
            created = api("POST", f"/tenants/{tenant}/ingestion-jobs/uploads", files=[
                ("files", (name, data, CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")))
                for name, data in files])
        except ApiError as e:
            st.session_state.upload_error = str(e)
            return None
    ref = {**created, "names": names, "doc_ids": [safe_stem(n) for n in names], "question": question}
    st.session_state.ingest_job = ref
    return ref


def render_ingest(ref: dict) -> None:
    """Status card: per-file ⏳/✅/❌ while indexing, stays as 'Ready' when done."""
    job = poll(ref)
    names = ref["names"]
    if "files_total" not in job:   # vanished job (API restart)
        st.status(f"Upload status unknown: {job.get('error', '')}", state="error")
        return
    failed = {Path(f["path"]).stem: f["error"] for f in job["failures"]}
    processed = job["files_done"] + job["files_failed"]
    running = job["status"] not in TERMINAL
    if running:
        label, state = f"Indexing {processed}/{len(names)}: {', '.join(names)}", "running"
    elif not failed:
        label, state = f"✅ Ready: {', '.join(names)}", "complete"
    else:
        label, state = f"{job['files_done']}/{len(names)} ready, {len(failed)} failed", "error"
    with st.status(label, state=state, expanded=running or bool(failed)):
        for i, name in enumerate(names):
            if safe_stem(name) in failed:
                st.markdown(f"❌ {md_escape(name)}: {md_escape(failed[safe_stem(name)])}")
            elif i < processed:
                st.markdown(f"✅ {md_escape(name)}: indexed")
            elif i == processed and running:
                st.markdown(f"⏳ {md_escape(name)}: reading and embedding…")
            else:
                st.markdown(f"🕓 {md_escape(name)}: queued")
        if job.get("error"):
            st.error(job["error"])


def handle_finished_upload(tenant: str) -> None:
    """Once per finished upload: tick the new documents and queue the question typed with them."""
    ref = st.session_state.get("ingest_job")
    if not ref or is_pending(ref) or ref.get("handled"):
        return
    ref["handled"] = True
    if ref["result"].get("files_done"):
        for doc_id in ref["doc_ids"]:
            st.session_state[f"doc-{tenant}-{doc_id}"] = True
        if ref.get("question"):
            st.session_state.pending_ask = ref["question"]


def graph_panel(tenant: str) -> None:
    """Graph app only: what the context graph contains, and a rebuild button."""
    try:
        g = api("GET", f"/tenants/{tenant}/graph")
    except ApiError as e:
        st.caption(f"🕸 graph unavailable: {e}")
        return
    if not g["built"]:
        st.warning("No context graph for this tenant yet. Add markdown documents, or rebuild.")
    with st.expander(f"🕸 Context graph · {g['nodes'].get('section', 0)} sections · "
                     f"{g['nodes'].get('entity', 0)} entities"):
        st.caption("**Edges** · " + " · ".join(f"{k}: {v}" for k, v in sorted(g["edges"].items())) or "none")
        if g["entities"]:
            st.caption("**Entities** · " + " · ".join(f"{k}: {v}" for k, v in sorted(g["entities"].items())))
        if st.button("Rebuild graph", use_container_width=True):
            with st.spinner("Rebuilding…"):
                try:
                    api("POST", f"/tenants/{tenant}/graph/rebuild")
                    st.toast("Context graph rebuilt")
                except ApiError as e:
                    st.error(str(e))
            st.rerun()


# --- app -----------------------------------------------------------------------

def main(cfg: UiConfig) -> None:
    st.set_page_config(page_title=cfg.title, page_icon=cfg.icon, layout="centered", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)

    # --- sidebar: workspace, documents, ingestion ---
    with st.sidebar:
        st.markdown(f"### {cfg.icon} {cfg.title}")
        if st.button("✏️  New chat", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        with st.expander("Workspace"):
            tenant = st.text_input("Tenant", value=cfg.default_tenant, help="Lowercase letters, digits, - and _")
            label = {"graph": "hybrid search + context graph (multi-hop)", "hybrid": "hybrid search",
                     "agent": "ReAct agent over hybrid search + graph tools (max 8 steps)"}[cfg.strategy]
            st.caption(f"Retrieval: **{label}**")
            try:
                h = api("GET", "/health")
                for name, state in h["checks"].items():
                    st.caption(f"{'🟢' if state == 'ok' else '🔴'} {name}: {state}")
            except ApiError as e:
                st.caption(f"🔴 {e}")
        if not TENANT_RE.match(tenant):
            st.error("Invalid tenant id")
            st.stop()

        handle_finished_upload(tenant)   # before the checkboxes, so new documents can start ticked

        st.markdown("**Documents**")
        try:
            docs = api("GET", f"/tenants/{tenant}/documents")
        except ApiError as e:
            docs = []
            st.error(str(e))
        st.caption("Tick documents to focus the chat; none ticked searches all." if docs
                   else "No documents yet. Attach files with 📎 in the message box.")
        selected = [d["doc_id"] for d in docs if st.checkbox(doc_label(d), key=f"doc-{tenant}-{d['doc_id']}",
                                                             help=d.get("company") or d.get("title"))]

        if docs:
            total_chunks = sum(d["chunks"] for d in docs)
            with st.expander(f"📚 In the index · {len(docs)} files · {total_chunks:,} chunks"):
                st.dataframe(
                    [{"File": d["doc_id"], "Document": doc_label(d), "Pages/sections": d["pages"],
                      "Chunks": d["chunks"], "Ingested": d["ingested_at"][:16].replace("T", " ") + " UTC"}
                     for d in docs],
                    hide_index=True, use_container_width=True,
                )
                empty = [d["doc_id"] for d in docs if not d["chunks"]]
                if empty:
                    st.warning(f"No chunks stored for: {', '.join(empty)}")

        if cfg.graph_panel:
            graph_panel(tenant)

        # Uploads start as soon as files are dropped; bumping the key clears the uploader afterwards.
        uploader_key = f"uploader-{st.session_state.setdefault('uploader_n', 0)}"
        dropped = st.file_uploader("Add documents", type=cfg.upload_types, accept_multiple_files=True, key=uploader_key)
        if dropped:
            start_upload(tenant, [(f.name, f.getvalue()) for f in dropped])
            st.session_state.uploader_n += 1
            st.rerun()
        if error := st.session_state.pop("upload_error", None):
            st.error(f"Upload failed: {error}")

        def sidebar_upload_status():
            ref = st.session_state.get("ingest_job")
            if not ref:
                return
            was_pending = is_pending(ref)
            try:
                render_ingest(ref)
            except ApiError as e:
                st.error(str(e))
                return
            if was_pending and not is_pending(ref):
                st.rerun()   # finished: refresh the document list, run follow-ups, stop polling

        live(sidebar_upload_status, pending=is_pending(st.session_state.get("ingest_job")))

    # --- chat ---
    messages: list[dict] = st.session_state.setdefault("messages", [])

    def submit_question(question: str) -> dict:
        """POST the question; returns the assistant message that will show the answer."""
        try:
            created = api("POST", f"/tenants/{tenant}/query-jobs", json={
                "question": question, "filters": {"doc_ids": selected or None}, "strategy": cfg.strategy})
            return {"role": "assistant", "job": created}
        except ApiError as e:
            return {"role": "assistant", "error": str(e)}

    def message_pending(m: dict) -> bool:
        return is_pending(m.get("job")) or is_pending(m.get("upload"))

    # Handle new input *before* drawing the thread, so the user's message is on screen
    # while the (slow) upload or POST runs below. chat_input is pinned to the bottom wherever it's called.
    scope = [doc_label(d) for d in docs if d["doc_id"] in selected]
    submitted = st.chat_input("Ask about your documents…", accept_file="multiple", file_type=cfg.upload_types)
    action = None
    if submitted and submitted.files:
        question = submitted.text.strip() or None
        messages.append({"role": "user", "content": question or "", "attachments": [f.name for f in submitted.files]})
        action = ("upload", [(f.name, f.getvalue()) for f in submitted.files], question)
    elif question := (submitted.text.strip() if submitted else "") or st.session_state.pop("pending_prompt", None):
        messages.append({"role": "user", "content": question, "scope": scope})
        action = ("ask", question)
    elif queued := st.session_state.pop("pending_ask", None):   # typed alongside an upload; already on screen
        action = ("ask", queued)

    if not messages:
        st.markdown(f"<div class='welcome'><h2>What would you like to know?</h2><p>{cfg.welcome}</p></div>",
                    unsafe_allow_html=True)
        for col, text in zip(st.columns(len(cfg.suggestions)), cfg.suggestions):
            if col.button(text, use_container_width=True):
                st.session_state.pending_prompt = text
                st.rerun()

    def conversation():
        was_pending = any(message_pending(m) for m in messages)
        for m in messages:
            with st.chat_message(m["role"], avatar="🧑" if m["role"] == "user" else cfg.icon):
                try:
                    if m["role"] == "user":
                        if m.get("content"):
                            st.markdown(md_escape(m["content"]))
                        if m.get("attachments"):
                            st.caption("📎 " + " · ".join(m["attachments"]))
                        else:
                            st.caption("in " + ", ".join(m["scope"]) if m.get("scope") else "in all documents")
                    elif "error" in m:
                        st.error(m["error"])
                    elif "upload" in m:
                        render_ingest(m["upload"])
                    else:
                        job = poll(m["job"])
                        if job["status"] in TERMINAL:
                            render_answer(job)
                        elif job.get("steps"):   # agent: show its steps live while it works
                            last = job["steps"][-1]
                            with st.status(f"Step {last['step']}: {_call(last)}", state="running", expanded=True):
                                render_trace(job["steps"])
                        else:
                            st.markdown(f":gray[_{cfg.thinking}_]")
                except ApiError as e:
                    st.error(str(e))
        if was_pending and not any(message_pending(m) for m in messages):
            st.rerun()   # everything finished: run follow-ups and rebuild without the polling timer

    live(conversation, pending=any(message_pending(m) for m in messages))

    if action:
        # The new user message is already drawn above; do the slow part under an assistant bubble.
        with st.chat_message("assistant", avatar=cfg.icon):
            if action[0] == "upload":
                _, files, question = action
                ref = start_upload(tenant, files, question)   # shows its own "Uploading …" spinner
                messages.append({"role": "assistant", "upload": ref} if ref else
                                {"role": "assistant", "error": f"Upload failed: {st.session_state.pop('upload_error', '')}"})
            else:
                with st.spinner("Sending your question…"):
                    messages.append(submit_question(action[1]))
        st.rerun()

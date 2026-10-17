---
name: ui-changes
description: Change the Streamlit apps (ui/core.py and the hybrid/graph/agent entry points) or their API contract. Use for any UI feature, layout or rendering fix.
---

# Streamlit UI changes

The apps are a Claude-like chat with a dark theme. `ui/core.py` holds everything shared; `hybrid.py`, `graph.py` and
`agent.py` are only `UiConfig`. Put features in `core.py` behind a config flag, never by forking the apps.
The UI talks only to the HTTP API.

## Rules learned the hard way

- **Escape `$`:** Streamlit renders `$...$` as LaTeX and mangles dollar figures. Answers escape only `$`, so markdown
  tables still work. Snippets escape all markdown.
- **Poll only while pending:** `live(fn, pending=...)` sets `run_every` only while a job is running. Endless polling
  makes "Running…" never stop.
- **Handle input before drawing the thread.** Otherwise the user's message disappears during the slow POST/upload.
- **Follow-ups after a job finishes run once, on the main page run** (`handle_finished_upload`). Two fragments watching
  the same job will race.
- **Vanished jobs** (404 after an API restart) count as failed, not as polling forever. Guard missing fields (`.get`).
- **Don't hide the toolbar:** it holds the expand-sidebar arrow. The sidebar starts expanded.
- **Always give feedback:** spinners while sending, a status card with per-file ✅/⏳/🕓/❌, and results stay visible.
- Uploads start right away, with metadata detected server-side. Content type follows the file extension.

## Testing

- Start a throwaway API on **:8001** (`API_URL=http://localhost:8001`) and use `streamlit.testing.v1.AppTest`. Clean
  up test tenants (`DELETE FROM documents WHERE tenant_id='demo'` and the upload folder) afterwards.
- AppTest **can't** attach files to `chat_input`, submit text to a file-accepting `chat_input`, or drive `file_uploader`.
  Seed `session_state` (`pending_prompt`, `ingest_job`, `messages`) to test those paths, and say what wasn't covered.
- Status elements show up as `type == "status"` in the element tree, not as expanders. Set
  `PYTHONIOENCODING=utf-8` when printing them.

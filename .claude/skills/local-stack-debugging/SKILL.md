---
name: local-stack-debugging
description: Diagnose freezes, slowness, crashes, port conflicts, GPU/RAM problems, Ollama or Docker issues on the local Windows stack. Use when something "hangs", "won't start" or is "taking forever".
---

# Debugging the local stack

**Measure before you guess.** Check processes, ports, memory and logs first, then form a hypothesis.

| Symptom | Check | Known causes |
|---|---|---|
| Ingest "froze" | process count, RAM, stderr | Nested process pools (spawn re-imports the main module): removed. Memory exhaustion looks like a freeze |
| `MemoryError`, "paging file too small" | `Win32_OperatingSystem` free RAM and commit | Moving the embedder GPU↔CPU; too many Python processes; Docker's WSL VM (capped at 4 GB in `~/.wslconfig`); an unknown `vmmem` VM |
| Port in use (`WinError 10048`) | `Get-NetTCPConnection -LocalPort 8000 -State Listen` | A leftover background uvicorn/streamlit. Kill only processes you started |
| API slow to bind | uvicorn log | Embedder warm-up blocking startup: now in a background thread |
| First query ~10 s | log line "loaded ... (bf16, cpu)" | Cold model load: warm-up exists in the API, not in one-shot CLI runs |
| CUDA OOM during ingest | `ollama ps`, `nvidia-smi` | LLM still loaded: ingest calls `unload_all()` under `GPU_LOCK` |
| Gemini/remote 429s | | Free-tier quotas count each batch item. That's why embeddings are local |
| Cursor terminals stuck | Cursor's pty host and GPU process age, `%APPDATA%\Cursor\logs` | Pty host hung after memory pressure. A full Cursor restart is needed (Reload Window doesn't restart the GPU process) |

## Habits

- Use `run_in_background` for servers, and poll with a bounded loop instead of sleeping blindly.
- Prefer a throwaway API on :8001 for checks; the user's own servers run on :8000/:850x.
- Before killing anything, identify it (command line, owner, age). Never touch processes you didn't start without
  saying why.
- PowerShell 5.1: no `&&`. Stderr from native exes shows as `NativeCommandError`; exit code 255 can be cosmetic.

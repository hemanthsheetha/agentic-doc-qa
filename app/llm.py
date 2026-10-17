import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
NUM_CTX = 8192

# One GPU (6 GB): GPU embedding (~3.3 GB peak) and the LLM (~3.9 GB) never run at the same time.
GPU_LOCK = threading.Lock()


def _post(path: str, payload: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        f"{OLLAMA_URL}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def unload_all() -> None:
    """Free GPU memory held by Ollama models. Best effort: no Ollama running is fine."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as resp:
            loaded = [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except (urllib.error.URLError, OSError) as e:
        logger.info("ollama not reachable (%s); nothing to unload", e)
        return
    for name in loaded:
        _post("/api/generate", {"model": name, "keep_alive": 0})
        logger.info("unloaded %s from GPU", name)
    if not loaded:
        logger.info("no ollama models loaded")


def chat(system: str, user: str, temperature: float = 0.1, timeout: float = 300, json_mode: bool = False,
         model: str | None = None) -> str:
    """Single-turn chat with the local LLM. Holds GPU_LOCK for the duration.

    json_mode constrains the output to valid JSON (Ollama `format: json`).
    """
    return chat_messages([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         temperature=temperature, timeout=timeout, json_mode=json_mode, model=model)


def chat_messages(messages: list[dict], temperature: float = 0.1, timeout: float = 300, json_mode: bool = False,
                  model: str | None = None) -> str:
    """Multi-turn chat (e.g. an agent loop). Holds GPU_LOCK for the duration of this one call."""
    payload = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_ctx": NUM_CTX, "temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"
    with GPU_LOCK:
        start = time.perf_counter()
        resp = _post("/api/chat", payload, timeout=timeout)
    out_tokens = resp.get("eval_count", 0)
    logger.info("llm %s: %d prompt + %d output tokens in %.1fs (%.0f tok/s)",
                payload["model"], resp.get("prompt_eval_count", 0), out_tokens, time.perf_counter() - start,
                out_tokens / max(resp.get("eval_duration", 1) / 1e9, 1e-9))
    return resp["message"]["content"].strip()

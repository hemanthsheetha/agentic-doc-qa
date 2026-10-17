import hashlib
import logging
import os
import threading
import time

import numpy as np

EMBED_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_DIM = 1024
BATCH_SIZE = 16                  # halved with MAX_SEQ_LEN doubled: same peak VRAM
MAX_SEQ_LEN = 1024               # 2000-char chunks are ~500 tokens + context; never truncate
GPU_MIN_TEXTS = 64               # bulk ingest goes to the GPU; single queries stay on CPU
QUERY_PROMPT = (
    "Instruct: Given a question about company financial filings, "
    "retrieve passages from the filings that answer it\nQuery: "
)

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    # Model name is part of the hash so switching models re-embeds everything.
    return hashlib.sha256(f"{EMBED_MODEL}\n{text}".encode("utf-8")).hexdigest()


_models: dict = {}
_models_lock = threading.Lock()


def _model(device: str):
    """One copy per device, loaded once and never moved.

    Moving weights between GPU and CPU needs a fresh RAM allocation each time,
    which fails on a memory-starved machine. Ingest loads on the GPU (released
    after ingest); query serving loads on the CPU, leaving the GPU to the LLM.
    """
    with _models_lock:  # API threads may ask concurrently; load once
        if device not in _models:
            import torch
            from sentence_transformers import SentenceTransformer

            start = time.perf_counter()
            model = SentenceTransformer(EMBED_MODEL, device=device, model_kwargs={"torch_dtype": torch.bfloat16})
            model.max_seq_length = MAX_SEQ_LEN
            _models[device] = model
            logger.info("loaded %s (bf16, %s) in %.1fs", EMBED_MODEL, device, time.perf_counter() - start)
        return _models[device]


def warm_up() -> None:
    """Load the CPU model and run one query, so the first real query isn't the slow one."""
    start = time.perf_counter()
    embed(["warm up"], "RETRIEVAL_QUERY")
    logger.info("query embedder warmed up in %.1fs", time.perf_counter() - start)


def release_gpu() -> None:
    """Drop the GPU copy of the model after bulk ingest so the LLM gets the VRAM back."""
    import torch

    with _models_lock:
        released = _models.pop("cuda", None) is not None
    if released and torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.info("released embedding model GPU memory (%.2f GB still allocated)",
                    torch.cuda.memory_allocated() / 1e9)


def _device(n_texts: int) -> str:
    import torch
    return "cuda" if n_texts >= GPU_MIN_TEXTS and torch.cuda.is_available() else "cpu"


def embed(texts: list[str], task_type: str) -> list[np.ndarray]:
    """task_type: 'RETRIEVAL_DOCUMENT' for chunks, 'RETRIEVAL_QUERY' for questions.

    Returns L2-normalized float32 vectors of length EMBED_DIM.
    """
    if not texts:
        return []
    device = _device(len(texts))
    prompt = QUERY_PROMPT if task_type == "RETRIEVAL_QUERY" else None

    start = time.perf_counter()
    vecs = _model(device).encode(
        texts,
        prompt=prompt,
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    elapsed = time.perf_counter() - start
    logger.info("embedded %d texts as %s on %s in %.2fs (%.0f texts/s)",
                len(texts), task_type, device, elapsed, len(texts) / elapsed)
    return list(vecs.astype(np.float32))

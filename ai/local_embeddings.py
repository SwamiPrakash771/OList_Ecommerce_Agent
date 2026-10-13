"""Optional local text embeddings for development and offline hybrid evaluations.

Model: sentence-transformers `paraphrase-multilingual-MiniLM-L12-v2` (English and Portuguese,
384 dimensions). Measured on this machine: ~70-90 ms per message on CPU after warm-up; loading
takes ~1-2 minutes once, at server start-up (ai.agent.warm_up). Vectors are normalised, so a dot
product is the cosine similarity. Set AI_LOCAL_EMBEDDINGS_ENABLED=false in production to disable
loading; production requirements do not install Torch or sentence-transformers.
"""

from __future__ import annotations

import os
import threading

import numpy as np

MODEL_NAME = os.environ.get("AI_LOCAL_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
THREADS = 4
ENABLED = os.environ.get("AI_LOCAL_EMBEDDINGS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

_model = None
_lock = threading.Lock()


def _load():
    global _model
    if not ENABLED:
        raise RuntimeError("Local embeddings are disabled by AI_LOCAL_EMBEDDINGS_ENABLED")
    with _lock:
        if _model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(THREADS)
            try:   # use the downloaded copy without contacting the hub
                _model = SentenceTransformer(MODEL_NAME, device="cpu", local_files_only=True)
            except Exception:
                _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def warm_up() -> None:
    embed(["warm up"])


def is_ready() -> bool:
    """Whether the optional model is enabled and loaded."""
    return ENABLED and _model is not None


_loading = threading.Event()


def load_in_background(on_load=None) -> None:
    """Start loading once, without blocking. Called when a hybrid question finds the model missing
    (e.g. the start-up warm-up failed because the database wasn't up yet); it retries on a later
    question if loading failed."""
    if not ENABLED or _model is not None or _loading.is_set():
        return
    _loading.set()

    def run():
        try:
            (on_load or warm_up)()
        except Exception:
            import logging

            logging.getLogger("ai.local_embeddings").exception("loading the local model failed")
        finally:
            _loading.clear()

    threading.Thread(target=run, daemon=True, name="local-model-load").start()


def embed(texts: list[str]) -> np.ndarray:
    model = _load()
    return np.asarray(model.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)

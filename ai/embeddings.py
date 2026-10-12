"""Text embeddings for the RAG router and answer cache.

Default: Google `gemini-embedding-001` (multilingual, ~11 tokens and ~180 ms per message, about
$0.15 per 1M tokens; see docs/RAG_PLAN.md section 4). Vectors are shortened to EMBED_DIMS and
normalised, so a dot product is the cosine similarity.

Embeddings of fixed texts (router examples) are cached on disk, keyed by model, size and text, so
a restart doesn't re-embed them. Messages are embedded once per turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import numpy as np

EMBED_MODEL = os.environ.get("AI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIMS = 768
TASK = "SEMANTIC_SIMILARITY"
CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "embedding_cache.json"

_client = None
_lock = threading.Lock()
_cache: dict[str, list[float]] | None = None


def _get_client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    return _client


def _key(text: str) -> str:
    return hashlib.sha256(f"{EMBED_MODEL}|{EMBED_DIMS}|{TASK}|{text}".encode()).hexdigest()


def _load_cache() -> dict[str, list[float]]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except (FileNotFoundError, ValueError):
            _cache = {}
    return _cache


def _normalise(vectors: list[list[float]]) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def _call(texts: list[str]) -> list[list[float]]:
    from google.genai import types

    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        r = _get_client().models.embed_content(
            model=EMBED_MODEL, contents=texts[i:i + 100],
            config=types.EmbedContentConfig(task_type=TASK, output_dimensionality=EMBED_DIMS))
        out.extend(e.values for e in r.embeddings)
    return out


def embed(texts: list[str], *, cache: bool = False) -> np.ndarray:
    """Normalised embeddings, one row per text. cache=True stores them on disk (fixed texts only,
    never customer messages)."""
    if not cache:
        return _normalise(_call(texts))
    with _lock:
        stored = _load_cache()
        missing = [t for t in dict.fromkeys(texts) if _key(t) not in stored]
        if missing:
            for text, vec in zip(missing, _call(missing)):
                stored[_key(text)] = vec
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(stored))
        return _normalise([stored[_key(t)] for t in texts])

"""Retrieval of checked SQL examples for query_my_data (few-shot RAG).

The two examples closest in meaning to the customer's message are added to the prompt when the
analytics pack is selected. Every example is run by tests/test_rag.py through the agent's own
validator and my_* tables, so only working SQL can be retrieved.
"""

from __future__ import annotations

import functools
from pathlib import Path

import numpy as np
import yaml

from ai import embeddings

EXAMPLES_FILE = Path(__file__).with_name("sql_examples.yaml")
TOP_N = 2


@functools.lru_cache(maxsize=1)
def examples() -> list[dict]:
    return yaml.safe_load(EXAMPLES_FILE.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def _matrix() -> np.ndarray:
    return embeddings.embed([e["q"] for e in examples()], cache=True)


def warm_up() -> None:
    _matrix()


def closest(vector: np.ndarray, n: int = TOP_N) -> list[dict]:
    scores = _matrix() @ vector
    return [examples()[i] for i in np.argsort(-scores)[:n]]


def prompt_section(vector: np.ndarray) -> str:
    parts = ["# Example queries (checked; adapt them, don't copy blindly)\n"]
    for e in closest(vector):
        parts.append(f"Question: {e['q']}\n{e['sql'].strip()}\n\n")
    return "".join(parts)

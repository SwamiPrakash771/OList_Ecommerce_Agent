"""RAG router: decides which tool packs (and so which prompt sections) a message needs.

The message is embedded and compared with labelled example questions (ai/router_examples.yaml).
Packs of the closest examples are selected; several close packs are combined. When the router
isn't confident, it selects every pack (today's full context), so a weak match costs tokens,
never a wrong answer. Follow-up messages keep the previous turn's packs.

Routing only narrows what the model sees: the tools still come from `tools_for(role)` and every
call is still checked by `call_tool`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from ai import embeddings
from ai.packs import ALL_PACKS

EXAMPLES_FILE = Path(__file__).with_name("router_examples.yaml")

MIN_CONFIDENCE = 0.75   # best match below this -> all packs (tuned with ai.router_eval)
MARGIN = 0.06           # packs whose best match is within this of the top one are added too
TOP_K = 8               # examples considered


@dataclass(frozen=True)
class Route:
    packs: frozenset[str] | None          # None = every pack (fallback)
    confidence: float
    reason: str
    matches: tuple[tuple[str, str, float], ...] = field(default=())   # (pack, example, score)

    @property
    def label(self) -> str:
        return "all" if self.packs is None else ",".join(sorted(self.packs)) or "none"


@functools.lru_cache(maxsize=1)
def _bank() -> tuple[list[str], list[str], np.ndarray]:
    data = yaml.safe_load(EXAMPLES_FILE.read_text(encoding="utf-8"))
    texts, labels = [], []
    for pack, examples in data.items():
        if pack != "none" and pack not in ALL_PACKS:
            raise ValueError(f"router example label '{pack}' is not a pack")
        for example in examples:
            texts.append(example)
            labels.append(pack)
    return texts, labels, embeddings.embed(texts, cache=True)


def warm_up() -> None:
    _bank()


def route_vector(vector: np.ndarray, previous: frozenset[str] | None = None) -> Route:
    texts, labels, matrix = _bank()
    scores = matrix @ vector
    order = np.argsort(-scores)[:TOP_K]
    matches = tuple((labels[i], texts[i], round(float(scores[i]), 3)) for i in order)
    best = float(scores[order[0]])
    if best < MIN_CONFIDENCE:
        return Route(None, best, "low confidence: all packs", matches)
    best_per_pack: dict[str, float] = {}
    for pack, _, score in matches:
        best_per_pack[pack] = max(best_per_pack.get(pack, -1.0), score)
    chosen = {p for p, s in best_per_pack.items() if s >= best - MARGIN}
    packs = frozenset(chosen - {"none"})
    reason = "matched"
    if previous:   # follow-ups ("add the first one") keep what the conversation was using
        packs |= previous
        reason = "matched + previous turn"
    return Route(packs, best, reason, matches)


def route(message: str, previous: frozenset[str] | None = None) -> Route:
    return route_vector(embeddings.embed([message])[0], previous)

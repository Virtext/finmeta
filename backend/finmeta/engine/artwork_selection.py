"""S9d4a: automatic artwork selection presets.

``artwork_selection`` (global config, optional per-library override) controls
which candidate is picked from the ranked candidate list for each artwork
kind when no manual override exists:

- ``best_rated`` (default): highest-rated candidate — TMDb ``vote_average``,
  with ``vote_count`` as tiebreak. Non-TMDb candidates have no rating and
  sort last, preserving the existing language/resolution ranking order.
- ``random_top_n``: pick among the N best-ranked candidates, but
  DETERMINISTICALLY: seed = hash(item_id, kind, reshuffle_count). The same
  inputs always produce the same choice (idempotent apply); only a
  reshuffle (which bumps the counter) changes it.
"""
from __future__ import annotations

import hashlib
from typing import List, Optional

from finmeta.sources.candidate import Candidate

PRESETS = ("best_rated", "random_top_n")


def rating_of(cand: Candidate) -> tuple[float, float]:
    """(vote_average, vote_count) for a candidate.

    TMDb candidates carry ``vote_average`` in ``score``; the count is not
    part of the shared Candidate model, so it stays 0 (a pure tiebreak).
    Non-TMDb sources report no rating at all.
    """
    if cand.source == "tmdb":
        return (float(cand.score or 0.0), 0.0)
    return (-1.0, -1.0)


def _seed_index(item_id: str, kind: str, reshuffle_count: int, n: int) -> int:
    """Deterministic index in [0, n) from (item_id, kind, reshuffle_count)."""
    payload = f"{item_id}|{kind}|{int(reshuffle_count)}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:12], 16) % max(1, n)


def select_candidate(
    ranked: List[Candidate],
    preset: str = "best_rated",
    top_n: int = 3,
    item_id: str = "",
    kind: str = "",
    reshuffle_count: int = 0,
) -> Optional[Candidate]:
    """Pick the winning candidate from a ranked (best-first) list.

    ``ranked`` must already be ordered best-first per the language/
    resolution ranking; this only re-orders by rating (best_rated) or picks
    deterministically among the top N (random_top_n).
    """
    if not ranked:
        return None
    if preset == "random_top_n":
        n = max(1, min(int(top_n), len(ranked)))
        pool = ranked[:n]
        return pool[_seed_index(item_id, kind, reshuffle_count, n)]
    # default / best_rated: highest rating, tiebreak vote_count, then the
    # existing ranking order (stable sort keeps equal ratings in place).
    return max(ranked, key=rating_of)


def effective_selection(global_sel: Optional[dict], lib_sel: Optional[dict]) -> dict:
    """Merge global + per-library artwork_selection; library wins when set."""
    g = dict(global_sel or {})
    l = dict(lib_sel or {})
    out = {
        "preset": g.get("preset", "best_rated"),
        "top_n": int(g.get("top_n", 3)),
    }
    if l:
        out["preset"] = l.get("preset", out["preset"])
        if l.get("top_n") is not None:
            out["top_n"] = int(l["top_n"])
    return out


__all__ = ["PRESETS", "select_candidate", "effective_selection", "rating_of"]

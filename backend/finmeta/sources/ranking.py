"""Ranking of candidates within a source (SPEC §4).

Preferred language > textless > other; then resolution; then the
source's own vote score.
"""
from __future__ import annotations

from typing import List, Optional

from finmeta.sources.candidate import Candidate


def _lang_rank(lang: Optional[str], prefs: List[Optional[str]]) -> int:
    """Lower is better. A listed language beats textless beats others."""
    # exact / base-2 match against a listed preference
    for i, pref in enumerate(prefs):
        if pref is None:
            continue
        if _base(lang) == _base(pref):
            return i
    # textless
    if lang is None:
        return prefs.index(None) if None in prefs else len(prefs)
    # otherwise after any listed preference (and after an explicit textless slot)
    return len(prefs)


def _base(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    # full locale (en-US) -> base (en)
    return code.split("-")[0].lower()


def rank_candidates(candidates: List[Candidate], prefs: List[Optional[str]]) -> List[Candidate]:
    """Return candidates sorted best-first.

    Sort key: language preference (asc), width (desc), height (desc),
    score (desc). Missing dimensions sort last.
    """

    def key(c: Candidate):
        return (
            _lang_rank(c.lang, prefs),
            -(c.width or 0),
            -(c.height or 0),
            -c.score,
        )

    return sorted(candidates, key=key)


def pick_best(candidates: List[Candidate], prefs: List[Optional[str]]) -> Optional[Candidate]:
    ranked = rank_candidates(candidates, prefs)
    return ranked[0] if ranked else None


__all__ = ["rank_candidates", "pick_best"]

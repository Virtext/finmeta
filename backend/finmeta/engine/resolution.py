"""Pick artwork candidates per configured source precedence (SPEC §4, §6).

For each artwork kind, iterate sources in precedence order. The first
source that yields a candidate wins. Within that source, candidates are
already ranked (language > resolution > score).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from finmeta.sources.candidate import Candidate
from finmeta.sources.tmdb import TmdbError

logger = logging.getLogger(__name__)


# S9d2-fix2 step 6b: provider errors must NOT be silent — callers pass a
# collector that accumulates {provider, error, count} entries per plan build.
WarningCollector = List[Dict[str, object]]


def _record_warning(warnings: Optional[WarningCollector], provider: str, exc: Exception) -> None:
    """Append one occurrence to the collector (None = no collector)."""
    if warnings is None:
        return
    for w in warnings:
        if w["provider"] == provider and w["error"] == str(exc):
            w["count"] += 1  # type: ignore[operator]
            return
    warnings.append({"provider": provider, "error": str(exc), "count": 1})


def _query_source(
    source_name: str,
    source_obj: object,
    media_type: str,
    tmdb_id: int,
    kind: str,
    language: Optional[str],
    lang_fallback: List[Optional[str]],
    warnings: Optional[WarningCollector] = None,
) -> List[Candidate]:
    """Query a single source for candidates of one kind.

    A source that errors (e.g. TMDb 401 from an invalid/placeholder key)
    degrades to "no candidates" instead of failing the whole plan build —
    matching already tolerates TmdbError, and artwork is optional per item.
    S9d2-fix2 step 6b: every error is recorded in ``warnings`` (when given)
    so the plan result can surface it instead of degrading silently."""
    if source_name == "tmdb":
        try:
            if media_type == "movie":
                return source_obj.movie_images(  # type: ignore[union-attr]
                    tmdb_id, language=language,
                    language_fallback=lang_fallback, kinds=[kind])
            return source_obj.tv_images(  # type: ignore[union-attr]
                tmdb_id, language=language,
                language_fallback=lang_fallback, kinds=[kind])
        except TmdbError as exc:
            logger.warning("tmdb candidates failed for %s/%s: %s", media_type, tmdb_id, exc)
            _record_warning(warnings, "tmdb", exc)
            return []
    if source_name == "fanart":
        return source_obj.candidates_for(media_type, tmdb_id, [kind])  # type: ignore[union-attr]
    if source_name == "local":
        return source_obj.candidates_for(tmdb_id, [kind])  # type: ignore[union-attr]
    return []


def resolve_candidates(
    sources: Dict[str, object],
    precedence: List[str],
    media_type: str,
    tmdb_id: int,
    kinds: List[str],
    language: Optional[str] = None,
    lang_fallback: Optional[List[Optional[str]]] = None,
    warnings: Optional[WarningCollector] = None,
) -> Dict[str, Candidate]:
    """Resolve one best candidate per artwork kind.

    Iterates ``kinds`` and for each, walks ``precedence`` in order.
    The first enabled source with a non-empty candidate list wins.
    Returns ``{kind: Candidate}``.
    """
    lang_fallback = lang_fallback or []
    result: Dict[str, Candidate] = {}
    for kind in kinds:
        for source_name in precedence:
            if source_name not in sources:
                continue
            cands = _query_source(
                source_name, sources[source_name], media_type, tmdb_id,
                kind, language, lang_fallback, warnings=warnings,
            )
            if cands:
                result[kind] = cands[0]
                break
    return result


__all__ = ["resolve_candidates", "collect_candidates"]


def collect_candidates(
    sources: Dict[str, object],
    precedence: List[str],
    media_type: str,
    tmdb_id: int,
    kinds: List[str],
    language: Optional[str] = None,
    lang_fallback: Optional[List[Optional[str]]] = None,
    warnings: Optional[WarningCollector] = None,
) -> Dict[str, List["Candidate"]]:
    """Gather *all* ranked candidates per kind across enabled sources.

    Unlike :func:`resolve_candidates` (which keeps only the single winner per
    kind), this returns the full ranked list per kind so a UI can offer the
    user a choice.
    """
    prefs: List[Optional[str]] = []
    if language:
        prefs.append(language)
    prefs.extend(lang_fallback or [])
    if not prefs:
        prefs = [None]
    from finmeta.sources.ranking import rank_candidates

    out: Dict[str, List[Candidate]] = {}
    for kind in kinds:
        acc: List[Candidate] = []
        for src_name in precedence:
            if src_name not in sources:
                continue
            acc.extend(_query_source(
                src_name, sources[src_name], media_type, tmdb_id,
                kind, language, lang_fallback, warnings=warnings,
            ))
        out[kind] = rank_candidates(acc, prefs)
    return out


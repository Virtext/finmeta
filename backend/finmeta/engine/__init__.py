"""Finmeta engine: matching, plan computation, NFO generation, diff."""
from finmeta.engine.plan import (
    ApiAction, FileWrite, ItemPlan, UnmatchedItem, WritePlan,
)
from finmeta.engine.matching import MatchResult, match_item
from finmeta.engine.nfo import movie_nfo, tvshow_nfo, season_nfo, episode_nfo
from finmeta.engine.diff import DiffEntry, diff_plan, diff_to_text
from finmeta.engine.resolution import resolve_candidates
from finmeta.engine.plan_builder import build_plan

__all__ = [
    "ApiAction", "FileWrite", "ItemPlan", "UnmatchedItem", "WritePlan",
    "MatchResult", "match_item",
    "movie_nfo", "tvshow_nfo", "season_nfo", "episode_nfo",
    "DiffEntry", "diff_plan", "diff_to_text",
    "resolve_candidates", "build_plan",
]
from finmeta.engine.format_rules import (
    DEFAULT_FORMAT_RULES,
detect_all,
detect_category,
default_format_rules,
)

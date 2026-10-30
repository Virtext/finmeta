"""Help registry for Finmeta (SPEC S9 §B).

Single source of truth for help text:
- Config field help lives in the Pydantic models (``description`` = one-line
  short, ``json_schema_extra["help"]`` = long markdown). We extract it here.
- Provider/feature help lives in the registries below.

``GET /api/help`` merges everything into one dict so the frontend never
hardcodes help text. A unit test fails if any config field or provider lacks
help.
"""
from __future__ import annotations

import typing
from typing import Any, Dict, Iterator, Tuple

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from finmeta.config.models import Config
from finmeta.config.secretspec import secret_specs


def _base_models(type_: Any) -> Iterator[type]:
    """Yield every BaseModel class reachable from a field annotation."""
    if type_ is None:
        return
    origin = typing.get_origin(type_)
    if origin is typing.Union:
        for a in typing.get_args(type_):
            if a is type(None):
                continue
            yield from _base_models(a)
        return
    if origin in (list, dict, set, frozenset, tuple):
        for a in typing.get_args(type_):
            yield from _base_models(a)
        return
    if origin is typing.Annotated:
        yield from _base_models(typing.get_args(type_)[0])
        return
    if isinstance(type_, type) and issubclass(type_, BaseModel):
        yield type_


def iter_config_fields(model_cls: type = Config, prefix: str = "",
                       _visited: set | None = None) -> Iterator[Tuple[str, FieldInfo]]:
    """Yield ``(dotted_path, FieldInfo)`` for every field of ``model_cls`` and
    of every reachable nested model."""
    if _visited is None:
        _visited = set()
    if model_cls in _visited:
        return
    _visited.add(model_cls)
    for name, info in model_cls.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        yield path, info
        for bm in _base_models(info.annotation):
            if bm is not model_cls:
                yield from iter_config_fields(bm, path, _visited)


def config_help(model_cls: type = Config) -> Dict[str, Dict[str, str]]:
    """Build ``{field_path: {"short": ..., "long": ...}}`` from the Pydantic
    model annotations (the single source of truth for config-field help)."""
    out: Dict[str, Dict[str, str]] = {}
    for path, info in iter_config_fields(model_cls):
        extra = info.json_schema_extra or {}
        out[path] = {"short": info.description or "", "long": extra.get("help", "")}
    return out


def config_fields_missing_help(model_cls: type = Config) -> list:
    """Return dotted paths of config fields that lack long help text."""
    missing = []
    for path, info in iter_config_fields(model_cls):
        extra = info.json_schema_extra or {}
        if not (extra.get("help") or "").strip():
            missing.append(path)
    return missing


# ---------------------------------------------------------------------------
# provider + feature help registry (long-form, English)
# ---------------------------------------------------------------------------

FEATURES: Dict[str, Dict[str, str]] = {
    "tab.general": {"short": "General settings",
                    "long": "Global switches that apply to every library: dry-run mode, the Jellyfin "
                            "server, path mappings, language and sync mode."},
    "tab.local": {"short": "Local services",
                  "long": "Services that run on or connect to your own infrastructure — today just "
                          "Jellyfin (URL + API key). Each has a Test button that reports connectivity "
                          "and, where possible, the server version. Sonarr/Radarr land in a later "
                          "session."},
    "tab.external": {"short": "External services",
                     "long": "Third-party metadata/artwork providers. Each has an enable toggle, a key "
                             "field (masked, write-only), a Test button, a 'get a key' link and a "
                             "rate-limit note. Only providers that exist today are shown here."},
    "tab.libraries": {"short": "Libraries",
                      "long": "Per-library profiles: which metadata and artwork to write, theme songs "
                              "and overlays. Each library is keyed by a name used by collections, "
                              "playlists and schedules."},
    "tab.advanced": {"short": "Advanced (raw YAML)",
                     "long": "The raw config YAML with live schema validation. Edits are validated "
                              "against the schema before applying; the previous config is kept so you "
                              "can undo. Use this for anything the forms don't expose yet."},
    # 2026-10-22 (S9h2): the remaining tabs never had a top-level "what is
    # this for" overview — only their individual fields did.
    "tab.ratings": {"short": "Rating sources",
                    "long": "For each rating badge (IMDb, Rotten Tomatoes, Metacritic, TMDb, "
                            "Letterboxd, Trakt), choose which provider supplies its value and in what "
                            "fallback order — the first enabled provider with a value wins. Reorder "
                            "with the arrows; disabled or unconfigured providers are greyed out and "
                            "skipped automatically."},
    "tab.overlays": {"short": "Overlays",
                     "long": "Configure what gets drawn on top of each poster: resolution/HDR/"
                             "audio-codec/rating badges, TV status ribbons, trending rank. Settings "
                             "are grouped into reusable profiles (style + which elements are on) that "
                             "a library points to via its `overlay_profile`; several libraries can "
                             "share one profile. Badge images come from two independent places: the "
                             "Kometa logo pack (a small fixed set of icons for ratings + the common "
                             "codec/resolution badges — install it once under \"Installed packs\") "
                             "and/or a locally imported Kometa pack (see \"Import Kometa overlay\" "
                             "below, for a richer set). Either way, a badge with no matching image "
                             "simply falls back to plain text — nothing ever breaks, it just looks "
                             "plainer."},
    "tab.collections": {"short": "Collections",
                        "long": "Built-in preset Jellyfin collections Finmeta creates and keeps in "
                                "sync for you: Top 50 Popular (movies/shows), Highest Rated (7.5+), "
                                "and Adaptive Seasonal Collections (a whole calendar of holiday "
                                "collections — Valentine's, Mother's/Father's Day, Halloween, "
                                "Thanksgiving, Christmas — each only present in Jellyfin during its "
                                "own window). These always need a full, all-libraries run, never a "
                                "single-library one — use the \"Apply (all libraries)\" button at the "
                                "bottom of this tab, or a schedule with no library restriction "
                                "(Schedules tab). Highest Rated and the genre/keyword-based seasonal "
                                "ones also need a working TMDb key (Settings > Secrets > tmdb_key) — "
                                "without one, TMDb lookups fail and those collections stay empty even "
                                "for titles that would clearly qualify, while Top 50 Popular still "
                                "works since it has no rating filter to fail on missing data."},
    "tab.schedules": {"short": "Schedules",
                      "long": "Recurring plan/apply runs on a cron schedule. Leave \"Libraries\" "
                              "empty to mean all configured libraries in one combined run — required "
                              "for the Collections tab's presets to ever sync automatically. Picking "
                              "specific libraries instead runs each one separately, which never "
                              "includes collections. A nightly run does not re-fetch everything from "
                              "scratch: TMDb lookups are cached (7 days for details, 30 for images) "
                              "and each item's chosen artwork is locked after its first successful "
                              "write, so a normal nightly apply only re-renders the overlay text/"
                              "badges on top of that locked image and only writes files that "
                              "actually changed."},
    "tab.trending": {"short": "Trending",
                     "long": "Mirrors public MDBList lists as Jellyfin collections, optionally "
                             "tagging the top 10 matched items with their rank as an overlay badge. "
                             "Needs an MDBList API key saved under Settings > Secrets."},
    "feature.kometa_import": {"short": "Download a known overlay pack",
        "long": "Brings in badge/overlay images (resolution, HDR, audio codec, edition, etc.) "
                "from a known third-party Kometa-style overlay source — Finmeta fetches it for "
                "you (no local copy needed first) and never ships or bundles these itself "
                "(they're other people's images, usually with no licence file). Pick a source "
                "from the list, give the pack a name, and run Preview first — it downloads the "
                "source, shows exactly which images would be copied, and flags any that are "
                "missing, without writing anything. Download & Import copies only the referenced "
                "images (plus a small `pack.yml` describing them) into "
                "`config/assets/packs/<pack name>/` — nothing else from the source is kept "
                "afterward. After importing, activate the pack for a profile under Overlays > "
                "Installed packs, and make sure the profile's elements (Resolution/HDR/Audio) "
                "are turned on. Note: not every Kometa-style source works — only ones that "
                "define their own image+condition per badge; sources that just reference "
                "Kometa's own built-in default overlays by name, or match via Plex collection "
                "membership, can't be translated and come back empty. Also: HDR/Audio/Edition "
                "badges are matched against the video file's filename (standard scene-release "
                "tags like `HDR10` or `Atmos`) — a plainly-named file (e.g. `Movie (2020).mkv`) "
                "won't get those badges no matter which pack is active. Only Resolution is "
                "detected from the actual video stream, independent of filename."},
    "auth": {"short": "Authentication",
             "long": "On first run you set an admin password (stored as a bcrypt hash in "
                     "`auth.yml`). Every `/api` route except `/api/health` and login requires a "
                     "session cookie (HttpOnly, SameSite=Strict). Login is rate-limited. Set "
                     "`FINMETA_AUTH=off` to disable for tests/dev."},
    "secrets": {"short": "Secrets",
                "long": "API keys are stored in `secrets.yml` (mode 0600), never in `config.yml`. If "
                        "the `FINMETA_SECRET_KEY` environment variable is set, values are "
                        "Fernet-encrypted at rest; otherwise they are stored in plaintext and a "
                        "warning is shown. The API never returns a key's value — only a masked hint."},
    "encrypted_at_rest": {"short": "Encryption at rest",
                          "long": "Whether secret values are Fernet-encrypted on disk. Turned on by "
                                  "setting the `FINMETA_SECRET_KEY` environment variable. When off, "
                                  "secrets are plaintext in `secrets.yml`."},
    "test_button": {"short": "Test",
                    "long": "Checks that the service is reachable and the key is valid, and reports "
                            "latency and (where available) the server/version. It never writes "
                            "anything."},
    "dry_run": {"short": "Dry run",
                "long": "While on (the default), apply runs only plan — no files are written and no "
                        "write API calls are made. Turn it off to make real changes."},
    "env_override": {"short": "Set via environment",
                     "long": "This value is provided by an environment variable, which takes "
                              "precedence over anything typed here. The field is locked because the "
                              "UI value would be ignored."},
    # 2026-10-22 (S9h1): longer-form narrative content for the "Setup Guide"
    # tab — not a single setting's tooltip, so it lives here rather than on
    # a config field.
    "guide.libraries": {"short": "Libraries",
        "long": "A \"library\" in Finmeta is a name you choose (e.g. `Movies`, `Shows`) that maps "
                "onto one real Jellyfin library and carries its own full settings: on/off, which "
                "metadata fields to write, which artwork kinds, an overlay profile, artwork "
                "selection, write method, theme song, Coming Soon. A Jellyfin library Finmeta "
                "doesn't know about is never touched — `enabled` defaults to off specifically so a "
                "newly-added Jellyfin library can't be written to by surprise. Collections, "
                "playlists and schedules all reference libraries by this chosen name, not "
                "Jellyfin's own internal id — renaming a library here breaks those references."},
    "guide.write_method": {"short": "Artwork write method: sidecar",
        "long": "`sidecar` is the only write method: Finmeta writes `poster.jpg`/`backdrop.jpg`/etc. "
                "straight into the media folder next to the video file — the same convention "
                "Kometa/TRaSH use; Jellyfin picks these up on its next scan. An alternative, "
                "`jellyfin_api`, used to upload the image directly into Jellyfin's own database over "
                "its API instead of touching the media folder. It was removed 2026-10-26: confirmed "
                "running against a real library that when the item's media folder already has a "
                "local poster/backdrop/logo/fanart file sitting next to the video (common for "
                "scene-release folders or anything previously scraped by Kodi/Emby-style local "
                "artwork), Jellyfin's own library scanning keeps re-asserting that local file — the "
                "upload \"succeeded\" every apply, but Jellyfin never actually served the new image. "
                "That made it unreliable for most real libraries, so sidecar is now the only option."},
}


def build_help(cfg) -> Dict[str, Dict[str, str]]:
    """Merge config-field help + provider help + feature help into one dict."""
    from finmeta.providers import provider_help_entries
    out: Dict[str, Dict[str, str]] = {}
    out.update(config_help(cfg.__class__))
    for name, prov in provider_help_entries().items():
        out[f"provider.{name}"] = {"short": prov["short"], "long": prov["long"]}
    for name, feat in FEATURES.items():
        out[f"feature.{name}"] = feat
    for spec in secret_specs(cfg):
        out[f"secret.{spec.name}"] = {"short": spec.short, "long": spec.long}
    return out


def providers_missing_help() -> list:
    """Return provider names that lack short/long help.

    Checks the real provider registry (``providers.provider_help_entries``)
    -- the same one ``build_help`` merges in -- not a separate hardcoded
    list, so this can't silently drift out of sync with what providers
    actually exist (a stale local copy covering only 4 of 7 real providers
    did exactly that until 2026-10-22; removed)."""
    from finmeta.providers import provider_help_entries
    missing = []
    for name, prov in provider_help_entries().items():
        if not (prov.get("short") or "").strip() or not (prov.get("long") or "").strip():
            missing.append(name)
    return missing


def features_missing_help() -> list:
    missing = [k for k, v in FEATURES.items()
               if not (v.get("short") or "").strip() or not (v.get("long") or "").strip()]
    return missing


__all__ = [
    "iter_config_fields", "config_help", "config_fields_missing_help",
    "FEATURES", "build_help",
    "providers_missing_help", "features_missing_help",
]

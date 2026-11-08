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
    # S10: library health scanner. The UI pulls these instead of hardcoding
    # "what does this check mean" text in the frontend (SPEC S9 §B).
    "page.health": {
        "short": "Library health",
        "long": "A read-only pass over your configured libraries that reports what is missing or "
                "wrong: artwork Finmeta can write, posters Jellyfin has not picked up, items it "
                "never probed, duplicate titles and libraries with no path mapping. Nothing is "
                "written until you click a fix. A scan uses the same single run slot as plan/apply, "
                "so an apply waits while one is running.",
    },
    "page.backup": {
        "short": "Backup & Restore",
        "long": "A backup bundle is one zip holding your DECISIONS: config.yml (so every library, "
                "overlay profile, schedule and threshold), plus the artwork choices that live "
                "outside it -- which candidate you picked, which image is locked, which kinds you "
                "skipped. It never contains secrets.yml, auth.yml, caches, the job database or "
                "media files; config.yml refers to API keys by name only, so a restored install "
                "needs those keys re-entered and the bundle tells you which ones. Pack images are "
                "not included either (they are large and re-downloadable) -- the bundle lists which "
                "packs to re-import. Inspect a bundle before restoring: it shows what would change "
                "without writing anything. A restore goes through the same validation as the "
                "Settings editor, so an invalid bundle is refused before anything is touched, and "
                "the previous config stays available through Undo.",
    },
    "page.activity": {
        "short": "Activity",
        "long": "Every plan, apply and scan run Finmeta has made, including the ones you did not "
                "start yourself: a nightly schedule, Jellyfin's webhook after a new episode, or "
                "another browser tab. Shows what is running now, what is queued behind it (one run "
                "happens at a time), when each schedule next fires, and the error/warning count per "
                "run so a failure is visible without opening anything. **Retry** runs a finished job "
                "again with exactly the same scope. A running job cannot be cancelled -- it is a "
                "background thread with no safe stopping point -- but a QUEUED one can be dropped "
                "before it starts.",
    },
    "scan.artwork": {
        "short": "Artwork presence",
        "long": "Checks the artwork kinds each library actually manages. Three different problems: "
                "**missing** (neither on disk nor in Jellyfin — Finmeta can download and write it), "
                "**not picked up** (the file is there but Jellyfin shows no image, so it needs to "
                "re-read the folder) and **image without a file** (Jellyfin has an image Finmeta "
                "owns no file for, so it cannot draw overlay badges on it).",
    },
    "scan.artwork_quality": {
        "short": "Artwork quality",
        "long": "Reads the header of each artwork file (no full decode) and reports images narrower "
                "than the configured floor, suspiciously small files (placeholders or truncated "
                "downloads) and files no image library can open. Low resolution is a 'pick another "
                "candidate' problem rather than a re-apply: artwork ranking already prefers the "
                "largest image, so a small poster usually means the source has nothing better.",
    },
    "scan.media": {
        "short": "Resolution and HDR information",
        "long": "Checks what Jellyfin knows about the video itself: items with no video stream or no "
                "height (never probed, or an unreadable file), 4K items with no dynamic-range "
                "information at all (the HDR badge silently disappears), and files whose NAME claims "
                "something the streams deny — e.g. `2160p HDR` on a 1080p SDR stream. Only Jellyfin "
                "can fix these by re-probing the file; Finmeta cannot invent stream data. Covers "
                "movies: a series carries no streams of its own.",
    },
    "scan.metadata": {
        "short": "Metadata",
        "long": "Checks the metadata on the Jellyfin item. A quick scan reports items with no "
                "TMDb/IMDb/TVDb id at all (nothing downstream works without one) and empty "
                "description/year/genre/studio fields. A deep scan adds the match checks: items "
                "TMDb cannot match, and items whose year is more than one off TMDb's -- which in "
                "practice means the item is matched to the wrong title. Finmeta can only fix these "
                "by writing an NFO, and only when the library is set to write NFOs; the provider id "
                "and the year live in Jellyfin's own database, so those link out instead.",
    },
    "scan.episodes": {
        "short": "Missing episodes",
        "long": "Deep scan only, and only with Sonarr configured. Asks Sonarr which monitored "
                "episodes have aired without a file, grouped per season, and flags a season with no "
                "files at all. Unaired episodes are never counted as missing, so a currently "
                "running season does not read as half-broken, and specials (season 0) are excluded "
                "unless you turn `include_specials` on. Finmeta never writes to Sonarr -- the "
                "finding links to the series there so you can trigger a search yourself.",
    },
    "scan.duplicates": {
        "short": "Duplicates",
        "long": "Reports items in one library that share a TMDb/IMDb id, two movies that resolve to "
                "the same folder (they would fight over the same poster.jpg) and items holding "
                "several versions, which Jellyfin usually treats as deliberate. Report-only: "
                "Finmeta never deletes or moves media files, so there is no fix button here — open "
                "the item in Jellyfin and decide yourself.",
    },
    "scan.path_unmapped": {
        "short": "Path mappings",
        "long": "Items whose Jellyfin path has no entry in `path_mappings`. This is reported as an "
                "error because every disk-side check is blind for those items, and an apply would "
                "skip them too. One finding per path prefix, not per item: it is a single config "
                "mistake. Fix it in Settings → path mappings.",
    },
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
                             "and/or a richer image pack placed by hand under "
                             "`config/assets/packs/<name>/` and activated under \"Installed packs\" "
                             "below. Either way, a badge with no matching image "
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

"""Hand-built image packs sourced from specific files in Kometa-Team/
Community-Configs -- NOT run through the generic expand_file/kometa_to_pack
zip-import pipeline (that only understands self-contained Kometa overlay YAML
like jmxd's; neither ichimoku nor pete2583's source fits that shape -- see
each entry's own pack_yml description below and STATE.md).

Each entry's ``files`` manifest (pack-relative path -> raw GitHub URL) was
verified against the real repo tree (GitHub Contents API, 2026-10-30) before
being added here -- every URL below is a real file at that exact path, never
guessed. Downloaded on demand via POST /api/kometa/import-remote (same
operator-initiated flow as jmxd) into the operator's own gitignored
``config/assets/packs/<name>/`` -- this module ships zero image bytes, only
the pack.yml metadata (category/matcher definitions) and a list of URLs.
"""
from __future__ import annotations

from typing import Any, Dict

ICHIMOKU_PACK_YML: Dict[str, Any] = {
    "name": "ichimoku",
    "description": (
        "Resolution/HDR/audio badges from Kometa-Team/Community-Configs ICHIMOKU "
        "(github.com/Kometa-Team/Community-Configs/tree/master/ICHIMOKU/overlays). "
        "Only the subset that maps onto Finmeta's own detection keys is included -- "
        "ICHIMOKU's combined badges (e.g. \"4K-HDR-DolbyTrueHDAtmos.png\") bake "
        "resolution+HDR+audio into one graphic and match on Plex collection "
        "membership, neither of which fits Finmeta's three-separate-badge model or "
        "its own file-metadata detection, so those were left out rather than wired "
        "in wrong."
    ),
    "categories": {
        "resolution": {"matcher": "format_rule", "keys": ["res_4k", "res_1080p"]},
        "dynamic_range": {"matcher": "format_rule", "keys": ["dv"]},
        "audio": {"matcher": "format_rule", "keys": ["truehd_atmos", "dtshd_ma"]},
    },
}

ICHIMOKU_FILES: Dict[str, str] = {
    "audio/dtshd_ma.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/ICHIMOKU/overlays/DTS-HD%20MA.png",
    "audio/truehd_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/ICHIMOKU/overlays/TrueHD%20Atmos.png",
    "dynamic_range/dv.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/ICHIMOKU/overlays/DV.png",
    "resolution/res_1080p.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/ICHIMOKU/overlays/1080p.png",
    "resolution/res_4k.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/ICHIMOKU/overlays/4K.png",
}

PETE2583_PACK_YML: Dict[str, Any] = {
    "name": "pete2583",
    "description": (
        "Combined resolution+HDR/DV+audio badges (one image per combination), from "
        "pete2583's Kometa overlay set (github.com/Kometa-Team/Community-Configs, "
        "pete2583/overlays). Images credited by pete2583: 1080p/DV/4K base images "
        "from @bradmarrti, 480p/576p/720p from the PMM defaults. Matched against "
        "real Jellyfin media-stream detection (Finmeta's own K9 rules), not "
        "filename guessing -- hand-built for Finmeta's format_rule_combo matcher, "
        "not run through the Kometa overlay importer (which has no notion of a "
        "multi-category combined badge). Not imported: the 4 IMAX variants (IMAX is "
        "an edition/filename concept, no K9 category for it) and the 8 "
        "resolution-less DV-* images (DV at 1080p/4K has no badge in this pack -- "
        "only 480p/576p/720p have DV coverage)."
    ),
    "categories": {
        "combined_resolution": {
            "matcher": "format_rule_combo",
            "components": ["resolution", "hdr", "audio"],
            "align": "top-left",
            "offset": [0, 0],
            "keys": [
                "res_1080p__hdr",
                "res_1080p__hdr__dd",
                "res_1080p__hdr__ddplus",
                "res_1080p__hdr__ddplus_atmos",
                "res_1080p__hdr__dtshd_ma",
                "res_1080p__hdr__dtsx",
                "res_1080p__hdr__truehd",
                "res_1080p__hdr__truehd_atmos",
                "res_1080p__sdr",
                "res_1080p__sdr__dd",
                "res_1080p__sdr__ddplus",
                "res_1080p__sdr__ddplus_atmos",
                "res_1080p__sdr__dtshd_ma",
                "res_1080p__sdr__dtsx",
                "res_1080p__sdr__truehd",
                "res_1080p__sdr__truehd_atmos",
                "res_480p__dv",
                "res_480p__hdr",
                "res_480p__sdr",
                "res_4k__hdr",
                "res_4k__hdr__dd",
                "res_4k__hdr__ddplus",
                "res_4k__hdr__ddplus_atmos",
                "res_4k__hdr__dtshd_ma",
                "res_4k__hdr__dtsx",
                "res_4k__hdr__truehd",
                "res_4k__hdr__truehd_atmos",
                "res_4k__sdr",
                "res_4k__sdr__dd",
                "res_4k__sdr__ddplus",
                "res_4k__sdr__ddplus_atmos",
                "res_4k__sdr__dtshd_ma",
                "res_4k__sdr__dtsx",
                "res_4k__sdr__truehd",
                "res_4k__sdr__truehd_atmos",
                "res_576p__dv",
                "res_576p__hdr",
                "res_576p__sdr",
                "res_720p__dv",
                "res_720p__hdr",
                "res_720p__sdr",
            ],
        },
    },
}

PETE2583_FILES: Dict[str, str] = {
    "images/combined_resolution/res_1080p__hdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR.png",
    "images/combined_resolution/res_1080p__hdr__dd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-DolbyDigital.png",
    "images/combined_resolution/res_1080p__hdr__ddplus.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-DolbyDigitalPlus.png",
    "images/combined_resolution/res_1080p__hdr__ddplus_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-DolbyDigitalPlusAtmos.png",
    "images/combined_resolution/res_1080p__hdr__dtshd_ma.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-dtsHD.png",
    "images/combined_resolution/res_1080p__hdr__dtsx.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-dtsX.png",
    "images/combined_resolution/res_1080p__hdr__truehd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-DolbyTrueHD.png",
    "images/combined_resolution/res_1080p__hdr__truehd_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-HDR-DolbyTrueHDAtmos.png",
    "images/combined_resolution/res_1080p__sdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p.png",
    "images/combined_resolution/res_1080p__sdr__dd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-DolbyDigital.png",
    "images/combined_resolution/res_1080p__sdr__ddplus.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-DolbyDigitalPlus.png",
    "images/combined_resolution/res_1080p__sdr__ddplus_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-DolbyDigitalPlusAtmos.png",
    "images/combined_resolution/res_1080p__sdr__dtshd_ma.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-dtsHD.png",
    "images/combined_resolution/res_1080p__sdr__dtsx.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-dtsX.png",
    "images/combined_resolution/res_1080p__sdr__truehd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-DolbyTrueHD.png",
    "images/combined_resolution/res_1080p__sdr__truehd_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/1080p-DolbyTrueHDAtmos.png",
    "images/combined_resolution/res_480p__dv.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/480P-DV.png",
    "images/combined_resolution/res_480p__hdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/480P-HDR.png",
    "images/combined_resolution/res_480p__sdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/480P.png",
    "images/combined_resolution/res_4k__hdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR.png",
    "images/combined_resolution/res_4k__hdr__dd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-DolbyDigital.png",
    "images/combined_resolution/res_4k__hdr__ddplus.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-DolbyDigitalPlus.png",
    "images/combined_resolution/res_4k__hdr__ddplus_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-DolbyDigitalPlusAtmos.png",
    "images/combined_resolution/res_4k__hdr__dtshd_ma.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-dtsHD.png",
    "images/combined_resolution/res_4k__hdr__dtsx.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-dtsX.png",
    "images/combined_resolution/res_4k__hdr__truehd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-DolbyTrueHD.png",
    "images/combined_resolution/res_4k__hdr__truehd_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-HDR-DolbyTrueHDAtmos.png",
    "images/combined_resolution/res_4k__sdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K.png",
    "images/combined_resolution/res_4k__sdr__dd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-DolbyDigital.png",
    "images/combined_resolution/res_4k__sdr__ddplus.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-DolbyDigitalPlus.png",
    "images/combined_resolution/res_4k__sdr__ddplus_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-DolbyDigitalPlusAtmos.png",
    "images/combined_resolution/res_4k__sdr__dtshd_ma.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-dtsHD.png",
    "images/combined_resolution/res_4k__sdr__dtsx.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-dtsX.png",
    "images/combined_resolution/res_4k__sdr__truehd.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-DolbyTrueHD.png",
    "images/combined_resolution/res_4k__sdr__truehd_atmos.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/4K-DolbyTrueHDAtmos.png",
    "images/combined_resolution/res_576p__dv.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/576P-DV.png",
    "images/combined_resolution/res_576p__hdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/576P-HDR.png",
    "images/combined_resolution/res_576p__sdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/576P.png",
    "images/combined_resolution/res_720p__dv.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/720P-DV.png",
    "images/combined_resolution/res_720p__hdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/720P-HDR.png",
    "images/combined_resolution/res_720p__sdr.png": "https://raw.githubusercontent.com/Kometa-Team/Community-Configs/master/pete2583/overlays/720P.png",
}

# Known-source entries consumed by routes_kometa.py -- same
# label/default_pack_name shape as KNOWN_SOURCES, plus pack_yml/files so the
# import-remote endpoint can write a pack without ever touching
# expand_file/kometa_to_pack (there is no Kometa overlay YAML to expand here).
MANIFEST_SOURCES: Dict[str, Dict[str, Any]] = {
    "ichimoku": {
        "label": "ichimoku (Kometa-Team/Community-Configs)",
        "default_pack_name": "ichimoku",
        "pack_yml": ICHIMOKU_PACK_YML,
        "files": ICHIMOKU_FILES,
    },
    "pete2583": {
        "label": "pete2583 combined badges (Kometa-Team/Community-Configs)",
        "default_pack_name": "pete2583",
        "pack_yml": PETE2583_PACK_YML,
        "files": PETE2583_FILES,
    },
}

__all__ = ["MANIFEST_SOURCES", "ICHIMOKU_PACK_YML", "ICHIMOKU_FILES", "PETE2583_PACK_YML", "PETE2583_FILES"]

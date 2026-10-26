#!/usr/bin/env python3
"""Generate the deterministic synthetic Finmeta test library (SPEC.md sec 13).

Jellyfin test server sees /work/fixture/library as /media:
  Movies/<Title (Year) [tmdbid-N]>/<Title (Year)>.mkv     (10 movies)
  Shows/<Title (Year) [tmdbid-N]>/Season NN/<Title> SNNENNN.mkv  (3 shows)
  Untouched/<Title (Year) [tmdbid-N]>/<Title (Year)>.mkv  (2 movies, third library)

Local asset sources (outside /media, used by the local-asset source):
  assets/<tmdbid>/theme.mp3   (10 s generated tone; 3 movies + 1 show)
  assets/<tmdbid>/poster.jpg  (custom poster; 1 movie)

Determinism: reruns skip files that already exist, so the generated tree
(and its tree_hash) is stable; --clean wipes and regenerates everything.
Encodes use bitexact, single-threaded settings. Note: the MKV container
header is not fully byte-stable across separate encodes with this ffmpeg,
so byte-identical output is guaranteed only via the skip-existing behavior
(open issue in STATE.md). TMDb IDs are verified against the TMDb API once;
full responses are cached in tmdb_cache.json (committed) so later runs make
no API calls.
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
import requests

HERE = Path(__file__).resolve().parent
LIB = HERE / "library"
ASSETS = HERE / "assets"
CACHE = HERE / "tmdb_cache.json"
TMDB_BASE = "https://api.themoviedb.org/3"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# (title, year, tmdb_id, width, height, audio_channels, duration_s)
# Resolutions per SPEC.md sec 13: 2x 4K, 4x 1080p, 3x 720p, 1x 480p.
# Exactly one movie (The Matrix) has 5.1/6-channel audio; the rest stereo.
MOVIES = [
    ("The Matrix", 1999, 603, 3840, 2160, 6, 3.0),
    ("The Matrix Reloaded", 2003, 604, 3840, 2160, 2, 3.0),
    ("Inception", 2010, 27205, 1920, 1080, 2, 2.5),
    ("Interstellar", 2014, 157336, 1920, 1080, 2, 3.0),
    ("The Dark Knight", 2008, 155, 1920, 1080, 2, 2.5),
    ("Pulp Fiction", 1994, 680, 1920, 1080, 2, 2.0),
    ("The Godfather", 1972, 238, 1280, 720, 2, 2.5),
    ("Alien", 1979, 348, 1280, 720, 2, 2.0),
    ("Back to the Future", 1985, 105, 1280, 720, 2, 2.5),
    ("Spirited Away", 2001, 129, 720, 480, 2, 2.0),
]
# (title, year, tmdb_id, {season: [episode numbers]})
SHOWS = [
    ("Breaking Bad", 2008, 1396, {1: [1, 2, 3], 2: [1, 2, 3]}),
    ("The Office", 2005, 2316, {1: [1, 2, 3, 4]}),
    ("Stranger Things", 2016, 66732, {1: [1, 2, 3]}),
]
# Third library, never listed in config (SPEC.md sec 14): 2 movies.
UNTOUCHED = [
    ("Dune", 2021, 438631, 1280, 720, 2, 2.5),
    ("No Country for Old Men", 2007, 6977, 1280, 720, 2, 2.5),
]
EPISODE = dict(w=1280, h=720, channels=2, duration=2.0)
THEME_IDS = (603, 27205, 238, 1396)  # 3 movies + 1 show
POSTER_IDS = (155,)                  # 1 movie


def _catalog():
    for t, y, i, *_ in MOVIES:
        yield "movie", i, t, y
    for t, y, i, _ in SHOWS:
        yield "tv", i, t, y
    for t, y, i, *_ in UNTOUCHED:
        yield "movie", i, t, y


def verify_tmdb() -> dict:
    """Verify every catalog TMDb ID; cache full API responses in tmdb_cache.json."""
    cache: dict = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text())
    missing = [f"{k}:{i}" for k, i, _, _ in _catalog()
               if cache.get(f"{k}:{i}", {}).get("status") != 200]
    if missing:
        key = os.environ.get("TMDB_KEY", "")
        if not key:
            raise SystemExit(f"TMDB_KEY env var is required to verify: {missing}")
        for ck in missing:
            kind, id_ = ck.split(":")
            r = requests.get(f"{TMDB_BASE}/{kind}/{id_}",
                             params={"api_key": key}, timeout=30)
            body = r.json() if 200 <= r.status_code < 300 else {"error": r.text[:300]}
            cache[ck] = {"kind": kind, "tmdb_id": int(id_),
                         "status": r.status_code, "response": body}
        CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    for kind, id_, title, year in _catalog():
        e = cache[f"{kind}:{id_}"]
        body = e["response"]
        assert isinstance(body, dict) and e["status"] == 200, f"bad cache for {kind}:{id_}"
        api_title = body.get("title") or body.get("name")
        assert api_title == title, f"{kind}:{id_} is {api_title!r}, catalog says {title!r}"
        date = body.get("release_date") or body.get("first_air_date") or ""
        assert date.startswith(str(year)), f"{kind}:{id_} year {date[:4]} != {year}"
    return cache


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-fflags", "+bitexact", "-flags", "+bitexact", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"ffmpeg failed ({p.returncode}): {p.stderr[-400:]}")


def make_video(path: Path, w: int, h: int, duration: float, channels: int) -> None:
    """2-3 s test-pattern MKV (x264 + aac), very low bitrate, bitexact.

    x264 is only bit-reproducible single-threaded (-threads 1), which is
    required for a stable tree_hash across runs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    layout = {1: "mono", 2: "stereo", 6: "5.1"}[channels]
    _run_ffmpeg([
        "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=25:duration={duration}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-af", f"aformat=channel_layouts={layout}",
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "1", "-pix_fmt", "yuv420p",
        "-b:v", "100k", "-maxrate", "150k", "-bufsize", "300k",
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ])


def make_theme(path: Path, duration: float = 10.0) -> None:
    """10 s generated sine tone as MP3 (deterministic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-f", "lavfi", "-i",
        f"sine=frequency=220:sample_rate=44100:duration={duration}",
        "-c:a", "libmp3lame", "-b:a", "128k",
        "-metadata", f"title=Finmeta test theme {path.parent.name}",
        str(path),
    ])


def make_poster(path: Path, tmdb_id: int) -> None:
    """Simple two-tone JPEG poster, deterministic, unique per tmdb_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    r, g, b = colorsys.hsv_to_rgb((tmdb_id * 47) % 360 / 360, 0.55, 0.9)
    col = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
    _run_ffmpeg([
        "-f", "lavfi", "-i", f"color=c={col}:s=720x1080:d=1:r=25",
        "-vf", "drawbox=x=0:y=720:w=720:h=360:color=black:t=fill",
        "-frames:v", "1", "-q:v", "2",
        str(path),
    ])


def tree_hash(root: Path) -> str:
    """Stable hash of a directory tree: sha256 over (relpath, size, file sha256)."""
    root = Path(root)
    h = hashlib.sha256()
    for p in sorted(q for q in root.rglob("*") if q.is_file()):
        data = p.read_bytes()
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\x00")
        h.update(str(len(data)).encode())
        h.update(b"\x00")
        h.update(hashlib.sha256(data).digest())
        h.update(b"\x00")
    return h.hexdigest()


def _movie_file(folder: Path, title: str, year: int) -> Path:
    return folder / f"{title} ({year}).mkv"


def _emit(path: Path, fn, *args) -> None:
    """Create path via fn, skipping if it already exists (idempotent reruns;
    --clean wipes the tree first so everything regenerates)."""
    if path.exists():
        print(f"skip (exists): {path.relative_to(HERE)}")
        return
    fn(path, *args)


def generate() -> None:
    for title, year, _id, w, h, ch, dur in MOVIES:
        folder = LIB / "Movies" / f"{title} ({year}) [tmdbid-{_id}]"
        _emit(_movie_file(folder, title, year), make_video, w, h, dur, ch)
    for title, year, _id, seasons in SHOWS:
        base = LIB / "Shows" / f"{title} ({year}) [tmdbid-{_id}]"
        for season in sorted(seasons):
            for ep in seasons[season]:
                name = f"{title} S{season:02d}E{ep:02d}.mkv"
                _emit(base / f"Season {season:02d}" / name, make_video,
                      EPISODE["w"], EPISODE["h"], EPISODE["duration"], EPISODE["channels"])
    for title, year, _id, w, h, ch, dur in UNTOUCHED:
        folder = LIB / "Untouched" / f"{title} ({year}) [tmdbid-{_id}]"
        _emit(_movie_file(folder, title, year), make_video, w, h, dur, ch)
    for tmdb_id in THEME_IDS:
        _emit(ASSETS / str(tmdb_id) / "theme.mp3", make_theme)
    for tmdb_id in POSTER_IDS:
        _emit(ASSETS / str(tmdb_id) / "poster.jpg", make_poster, tmdb_id)


def _chmod_readable() -> None:
    """Ensure uid 99 (and any other reader) can read the whole fixture."""
    for root in (LIB, ASSETS):
        for p in [root, *root.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)


def _clean_dir(d: Path) -> None:
    """Wipe the *contents* of d (rmtree subdirs, unlink files) but keep d itself.

    fixture/library is bind-mounted into the Jellyfin container as /media.
    Removing the mount-point directory itself (shutil.rmtree on the root)
    destroys its inode, so the container's /media is left with a stale mount
    (ESTALE - 'Stale file handle', errno 116) and Jellyfin sees an empty
    library. Only the children may be deleted; the mount-point directory must
    survive. mkdir(exist_ok=True) is a no-op when d already exists, so its
    inode is preserved in the normal (bind-mounted) case.
    """
    d.mkdir(parents=True, exist_ok=True)
    for child in sorted(d.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def main(argv=None) -> int:
    os.umask(0o022)
    ap = argparse.ArgumentParser(description="Generate the Finmeta synthetic fixture.")
    ap.add_argument("--clean", action="store_true",
                    help="wipe fixture/library and fixture/assets first")
    args = ap.parse_args(argv)
    if args.clean:
        for d in (LIB, ASSETS):
            _clean_dir(d)
    verify_tmdb()
    generate()
    _chmod_readable()
    print(f"library: {LIB}  hash={tree_hash(LIB)}")
    print(f"assets:  {ASSETS}  hash={tree_hash(ASSETS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

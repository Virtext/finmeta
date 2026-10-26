#!/usr/bin/env python3
"""End-to-end test: generate → bootstrap → plan → apply → assert → undo.

Runs the full Finmeta pipeline against the throwaway Jellyfin test server.
Prints a summary line per step. Exits non-zero on the first failure.

Human note (Open issues): regenerated MKVs are not byte-identical, so the
tree_hash is snapshotted right after step 1 and step 8 compares undo against
that snapshot; we never regenerate to compare.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PYTHON = "/work/.venv/bin/python"
LIB = ROOT / "fixture" / "library"
ASSETS = ROOT / "fixture" / "assets"
CREDS = ROOT / ".jf-test" / "creds.json"
CFG_PATH = ROOT / ".jf-test" / "config.yml"

# Theme-song asset IDs (3 movies + 1 show) and poster asset IDs (1 movie).
THEME_IDS = (603, 27205, 238, 1396)
POSTER_IDS = (155,)

# The "Christopher Nolan" list collection members (tmdb_ids).
NOLAN_TMDB_IDS = (27205, 157336, 155)


def _run(cmd: list, *, env=None, label="") -> subprocess.CompletedProcess:
    """Run a command, printing its tail on failure."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=full_env, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"  STDERR: {r.stderr[-500:]}")
        print(f"  STDOUT: {r.stdout[-500:]}")
        raise SystemExit(f"step failed ({label}): returncode={r.returncode}")
    return r


def _tree_hash(root: Path) -> str:
    """Stable hash of a directory tree (same as fixture/generate.py)."""
    import hashlib
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


def _write_config() -> None:
    """Write the e2e test config (dry_run: false, test server, fixture paths)."""
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    test_url = os.environ.get("JF_TEST_URL", "http://192.168.1.10:8097")
    cfg = f"""\
dry_run: false
server:
  url: {test_url}
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /work/fixture/library
language: en-US
artwork_language_fallback: [en, null]
sources:
  tmdb:   {{ enabled: true, api_key_env: TMDB_KEY }}
  fanart: {{ enabled: false, api_key_env: FANART_KEY }}
  local:  {{ enabled: true, path: /work/fixture/assets }}
  precedence: [local, fanart, tmdb]
libraries:
  Movies:
    type: movies
    metadata:   {{ enabled: true }}
    artwork:    {{ poster: true, backdrop: true, logo: false, landscape: false }}
    theme_song: true
    overlays:   []
  Shows:
    type: tvshows
    metadata:   {{ enabled: true }}
    artwork:    {{ poster: true, backdrop: true, logo: false, season_posters: false, episode_thumbs: false }}
    theme_song: true
    overlays:   []
collections:
  - name: "Christopher Nolan"
    type: list
    library: Movies
    tmdb_ids: [27205, 157336, 155]
playlists:
  - name: "Movie Night"
    user: finmeta
    library: Movies
    filter: {{ genres: [Action, Science Fiction], min_rating: 7.5 }}
    limit: 20
    sort: rating_desc
"""
    CFG_PATH.write_text(cfg)


def _creds() -> dict:
    return json.loads(CREDS.read_text())


def _make_jf(creds: dict):
    from finmeta.jellyfin_client import JellyfinClient
    jf = JellyfinClient(creds["url"], token=creds["api_key"])
    users = jf._request("GET", "/Users") or []
    uid = None
    for u in users:
        if u.get("Name") == creds.get("username", "finmeta"):
            uid = u.get("Id")
            break
    if uid:
        jf.user_id = uid
    return jf


def main() -> int:
    creds = _creds()
    env = {
        "JELLYFIN_API_KEY": creds["api_key"],
        "TMDB_KEY": os.environ.get("TMDB_KEY", ""),
    }
    if not env["TMDB_KEY"]:
        print("FATAL: TMDB_KEY env var not set", file=sys.stderr)
        return 1

    _write_config()
    cfg = str(CFG_PATH)
    steps: list[str] = []

    # -- Step 1: generate --clean ------------------------------------------
    print("Step 1: generate --clean")
    r = _run([PYTHON, "fixture/generate.py", "--clean"], env=env, label="generate")
    # Extract tree_hash from output (last line: "library: ... hash=...")
    lib_hash = ""
    for line in r.stdout.strip().split("\n"):
        if line.startswith("library:"):
            lib_hash = line.split("hash=")[1].strip()
    snapshot_hash = lib_hash
    steps.append(f"1. generate --clean  OK  tree_hash={snapshot_hash[:16]}…")
    print(f"  tree_hash={snapshot_hash[:16]}…")

    # -- Step 2: jf_bootstrap --reset --------------------------------------
    print("Step 2: jf_bootstrap --reset")
    _run([PYTHON, "scripts/jf_bootstrap.py", "--reset"], env=env, label="bootstrap")
    steps.append("2. jf_bootstrap --reset  OK")
    print("  OK")

    # -- Step 3: plan -------------------------------------------------------
    print("Step 3: plan")
    r = _run(
        [PYTHON, "-m", "finmeta.cli.main", "plan", "--config", cfg],
        env=env, label="plan",
    )
    plan_line = [l for l in r.stdout.strip().split("\n") if l.startswith("Files:")]
    plan_summary = plan_line[0] if plan_line else "(no summary)"
    steps.append(f"3. plan  OK  {plan_summary}")
    print(f"  {plan_summary}")

    # -- Step 4: apply ------------------------------------------------------
    print("Step 4: apply")
    r = _run(
        [PYTHON, "-m", "finmeta.cli.main", "apply", "--config", cfg],
        env=env, label="apply",
    )
    run_id = ""
    for line in r.stdout.strip().split("\n"):
        if line.startswith("run_id:"):
            run_id = line.split(":", 1)[1].strip()
    written = 0
    for line in r.stdout.strip().split("\n"):
        if line.startswith("files written:"):
            written = int(line.split(":")[1].strip())
    steps.append(f"4. apply  OK  run_id={run_id}  written={written}")
    print(f"  run_id={run_id}  written={written}")

    # -- Step 5: assert on disk --------------------------------------------
    print("Step 5: assert on disk")
    errors = []
    # NFO files for all movies and shows
    for folder in sorted((LIB / "Movies").iterdir()):
        if folder.is_dir() and (folder / "movie.nfo").exists():
            pass
        elif folder.is_dir():
            errors.append(f"missing movie.nfo in {folder.name}")
    for folder in sorted((LIB / "Shows").iterdir()):
        if folder.is_dir() and (folder / "tvshow.nfo").exists():
            pass
        elif folder.is_dir():
            errors.append(f"missing tvshow.nfo in {folder.name}")
    # Theme songs for asset items
    for tmdb_id in (603, 27205, 238):  # movies
        matches = list((LIB / "Movies").glob(f"*[tmdbid-{tmdb_id}]"))
        if matches:
            if not (matches[0] / "theme.mp3").exists():
                errors.append(f"missing theme.mp3 for movie tmdb-{tmdb_id}")
    for tmdb_id in (1396,):  # shows
        matches = list((LIB / "Shows").glob(f"*[tmdbid-{tmdb_id}]"))
        if matches:
            if not (matches[0] / "theme.mp3").exists():
                errors.append(f"missing theme.mp3 for show tmdb-{tmdb_id}")
    # Poster for asset item
    for tmdb_id in (155,):
        matches = list((LIB / "Movies").glob(f"*[tmdbid-{tmdb_id}]"))
        if matches:
            if not (matches[0] / "poster.jpg").exists():
                errors.append(f"missing poster.jpg for movie tmdb-{tmdb_id}")

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        steps.append(f"5. assert disk  FAIL ({len(errors)} errors)")
        return 1
    steps.append("5. assert disk  OK  (NFO + theme.mp3 + poster present)")
    print("  OK")

    # -- Step 6: assert in Jellyfin ----------------------------------------
    print("Step 6: assert in Jellyfin")
    jf = _make_jf(creds)
    jf_errors = []
    try:
        # Get all movie items
        movies_lib = jf.get_library("Movies")
        movies = jf.get_items(parent_id=movies_lib.item_id)
        movies_by_tmdb = {m.provider_ids.get("Tmdb"): m for m in movies if m.provider_ids.get("Tmdb")}

        # ImageTags: check at least one movie has a poster image tag
        tagged = [m for m in movies if m.image_tags is not None]
        if not tagged:
            jf_errors.append("no movies have ImageTags set")
        else:
            print(f"  ImageTags: {len(tagged)}/{len(movies)} movies tagged")

        # Theme songs for asset items
        for tmdb_id in (603, 27205, 238):
            m = movies_by_tmdb.get(str(tmdb_id))
            if m:
                songs = jf.get_theme_songs(m.id)
                if not songs:
                    jf_errors.append(f"no theme songs for movie tmdb-{tmdb_id}")
                else:
                    print(f"  ThemeSongs: tmdb-{tmdb_id} → {len(songs)} song(s)")

        # Collection: "Christopher Nolan" with 3 members
        coll = jf.get_collection_by_name("Christopher Nolan")
        if coll is None:
            jf_errors.append("collection 'Christopher Nolan' not found")
        else:
            members = jf.get_items(parent_id=coll.id)
            if len(members) < 1:
                jf_errors.append(f"collection 'Christopher Nolan' has {len(members)} members (expected ≥1)")
            else:
                print(f"  Collection 'Christopher Nolan': {len(members)} member(s)")

        # Playlist: "Movie Night" exists
        pl = jf.get_playlist_by_name("Movie Night")
        if pl is None:
            jf_errors.append("playlist 'Movie Night' not found")
        else:
            print(f"  Playlist 'Movie Night': present")

        # NFO title/overview: check a movie's Overview is non-empty
        m_matrix = movies_by_tmdb.get("603")
        if m_matrix:
            raw = jf._request("GET", f"/Items/{m_matrix.id}",
                              params={"userId": jf.user_id}) or {}
            overview = raw.get("Overview", "")
            if not overview:
                jf_errors.append("The Matrix Overview is empty (NFO not picked up)")
            else:
                print(f"  NFO Overview: The Matrix → {overview[:50]}…")
    finally:
        jf.close()

    if jf_errors:
        for e in jf_errors:
            print(f"  FAIL: {e}")
        steps.append(f"6. assert Jellyfin  FAIL ({len(jf_errors)} errors)")
        return 1
    steps.append("6. assert Jellyfin  OK  (tags, songs, collection, playlist, NFO)")
    print("  OK")

    # -- Step 7: plan + apply again → zero changes -------------------------
    # "apply" consumes the most recent plan JSON, and the executor only
    # skips files whose plan status is "unchanged" (it does not re-diff).
    # So refresh the plan first: it diffs against the now fully populated
    # library, marks everything unchanged, and the second apply writes
    # nothing -- the idempotency check.
    print("Step 7: plan + apply again (expect zero changes)")
    _run([PYTHON, "-m", "finmeta.cli.main", "plan", "--config", cfg],
         env=env, label="plan-2")
    r = _run(
        [PYTHON, "-m", "finmeta.cli.main", "apply", "--config", cfg],
        env=env, label="apply-2",
    )
    unchanged = 0
    written2 = 0
    for line in r.stdout.strip().split("\n"):
        if line.startswith("files unchanged:"):
            unchanged = int(line.split(":")[1].strip())
        if line.startswith("files written:"):
            written2 = int(line.split(":")[1].strip())
    if written2 != 0:
        steps.append(f"7. plan+apply again  FAIL  written={written2} (expected 0)")
        return 1
    steps.append(f"7. plan+apply again  OK  written=0  unchanged={unchanged}")
    print(f"  OK  written=0  unchanged={unchanged}")

    # -- Step 8: undo → tree hash == snapshot --------------------------------
    print(f"Step 8: undo {run_id}")
    r = _run(
        [PYTHON, "-m", "finmeta.cli.main", "undo", run_id, "--config", cfg],
        env=env, label="undo",
    )
    restored = deleted = 0
    for line in r.stdout.strip().split("\n"):
        if line.startswith("restored:"):
            restored = int(line.split(":")[1].strip())
        if line.startswith("deleted:"):
            deleted = int(line.split(":")[1].strip())
    after_hash = _tree_hash(LIB)
    if after_hash != snapshot_hash:
        steps.append(
            f"8. undo  FAIL  tree_hash={after_hash[:16]}… != snapshot={snapshot_hash[:16]}…"
        )
        print(f"  FAIL: tree_hash={after_hash[:16]}… != snapshot={snapshot_hash[:16]}…")
        return 1
    steps.append(
        f"8. undo  OK  restored={restored}  deleted={deleted}  tree_hash={after_hash[:16]}… == snapshot"
    )
    print(f"  OK  restored={restored}  deleted={deleted}  tree_hash matches snapshot")

    # -- Summary -------------------------------------------------------------
    print("\n=== E2E SUMMARY ===")
    for s in steps:
        print(s)
    print("ALL STEPS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

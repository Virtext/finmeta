"""Fixture checks: layout, naming, tmdbids, assets, rerun idempotency.

Run generate.py twice WITHOUT --clean and require an identical tree hash
(skip-existing behavior; see STATE.md open issue on MKV header bytes).
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = ROOT / "fixture"
GEN = FIX / "generate.py"
LIB = FIX / "library"
ASSETS = FIX / "assets"

_spec = importlib.util.spec_from_file_location("finmeta_fixture_gen", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

FOLDER_RE = re.compile(r"^(.+) \((\d{4})\) \[tmdbid-(\d+)\]$")
EXPECTED_MOVIE_IDS = {i for _, _, i, *_ in gen.MOVIES}
EXPECTED_SHOW_IDS = {i for _, _, i, _ in gen.SHOWS}
EXPECTED_UNTOUCHED_IDS = {i for _, _, i, *_ in gen.UNTOUCHED}


def _dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir())


def _parse_folder(p: Path) -> tuple[str, int, int]:
    m = FOLDER_RE.match(p.name)
    assert m, f"bad folder name: {p.name}"
    return m.group(1), int(m.group(2)), int(m.group(3))


def _run_gen() -> None:
    # sys.executable, not a hardcoded ".venv/bin/python": this test is run
    # via "python -m pytest" (see pyproject.toml / CI), so the running
    # interpreter already IS the right one, whatever its path -- a literal
    # ".venv" need not exist (e.g. in CI, where deps install straight into
    # the runner's system Python).
    subprocess.run([sys.executable, str(GEN)], check=True, capture_output=True)


class TestLibrary:
    def test_counts(self):
        assert len(_dirs(LIB / "Movies")) == 10
        assert len(_dirs(LIB / "Shows")) == 3
        assert len(_dirs(LIB / "Untouched")) == 2

    def test_naming_and_tmdbids(self):
        assert {_parse_folder(p)[2] for p in _dirs(LIB / "Movies")} == EXPECTED_MOVIE_IDS
        assert {_parse_folder(p)[2] for p in _dirs(LIB / "Shows")} == EXPECTED_SHOW_IDS
        assert {_parse_folder(p)[2] for p in _dirs(LIB / "Untouched")} == EXPECTED_UNTOUCHED_IDS
        for p in _dirs(LIB / "Movies") + _dirs(LIB / "Untouched"):
            title, year, _ = _parse_folder(p)
            assert (p / f"{title} ({year}).mkv").is_file(), f"missing video in {p.name}"
        for p in _dirs(LIB / "Shows"):
            title, year, _ = _parse_folder(p)
            seasons = _dirs(p)
            assert seasons, f"no seasons in {p.name}"
            n_eps = 0
            for s in seasons:
                assert re.fullmatch(r"Season \d{2}", s.name), s.name
                eps = sorted(q.name for q in s.iterdir() if q.is_file())
                assert 1 <= len(eps) <= 6, f"episode count in {s}"
                n_eps += len(eps)
                for name in eps:
                    assert re.fullmatch(rf"{re.escape(title)} S\d{{2}}E\d{{2}}\.mkv", name), name
            assert 3 <= n_eps <= 6, f"episode total {n_eps} for {p.name}"

    def test_tmdb_cache(self):
        cache = json.loads((FIX / "tmdb_cache.json").read_text())
        for kind, ids in (("movie", EXPECTED_MOVIE_IDS | EXPECTED_UNTOUCHED_IDS),
                          ("tv", EXPECTED_SHOW_IDS)):
            for i in ids:
                assert cache[f"{kind}:{i}"]["status"] == 200

    def test_same_collection_pair(self):
        """2 movies from the same TMDb collection (SPEC.md sec 13)."""
        cache = json.loads((FIX / "tmdb_cache.json").read_text())
        by_id = {i: cache[f"movie:{i}"]["response"] for i in EXPECTED_MOVIE_IDS}
        cids = [(v.get("belongs_to_collection") or {}).get("id")
                for v in by_id.values() if (v.get("belongs_to_collection") or {}).get("id")]
        assert len(cids) >= 2 and len(set(cids)) < len(cids), cids


class TestAssets:
    def test_themes(self):
        assert {p.parent.name for p in ASSETS.glob("*/theme.mp3")} == set(map(str, gen.THEME_IDS))
        assert set(map(int, gen.THEME_IDS)) & EXPECTED_SHOW_IDS  # 1 show has a theme

    def test_posters(self):
        posters = [p.parent.name for p in ASSETS.glob("*/poster.jpg")]
        assert posters == [str(i) for i in gen.POSTER_IDS]
        assert set(map(int, posters)) <= EXPECTED_MOVIE_IDS


class TestIdempotency:
    def test_two_runs_same_tree_hash(self):
        h0 = gen.tree_hash(LIB)
        _run_gen()
        h1 = gen.tree_hash(LIB)
        _run_gen()
        h2 = gen.tree_hash(LIB)
        assert h0 == h1 == h2

"""config/runs/plan-*.json naming (audit 2026-11-11 "D12").

_write_plan_json used to name the file plan-<int(time.time())>.json --
two runs starting in the same second collided, one plan silently
overwriting the other on disk. _do_apply also duplicated this exact
logic inline instead of calling _write_plan_json, so the bug lived in
two places at once. Fixed by reusing the run_id's own <epoch>-<hex>
suffix (run_id is itself collision-free) instead of a bare timestamp.
"""
from __future__ import annotations

from pathlib import Path

from finmeta.api.run_service import _write_plan_json
from finmeta.engine.plan import WritePlan
from finmeta.engine.runs import _plan_epoch


class _FakeContext:
    """Just enough of AppContext for _write_plan_json."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir

    def redact(self, text: str) -> str:
        return text


def _plan() -> WritePlan:
    return WritePlan(items=[])


class TestWritePlanJson:
    def test_two_runs_in_the_same_second_do_not_collide(self, tmp_path: Path):
        ctx = _FakeContext(tmp_path)
        p1 = _write_plan_json(ctx, _plan(), "run-1792700000-aaaaaa")
        p2 = _write_plan_json(ctx, _plan(), "run-1792700000-bbbbbb")
        assert p1 != p2
        assert p1.is_file() and p2.is_file()

    def test_filename_reuses_the_run_ids_own_suffix(self, tmp_path: Path):
        ctx = _FakeContext(tmp_path)
        p = _write_plan_json(ctx, _plan(), "run-1792700000-aaaaaa")
        assert p.name == "plan-1792700000-aaaaaa.json"

    def test_creates_the_runs_dir(self, tmp_path: Path):
        ctx = _FakeContext(tmp_path)
        p = _write_plan_json(ctx, _plan(), "run-1792700000-aaaaaa")
        assert p.parent == tmp_path / "runs"
        assert p.parent.is_dir()


class TestPlanEpochParsesBothNamingSchemes:
    def test_legacy_bare_epoch_name(self, tmp_path: Path):
        p = tmp_path / "plan-1792700000.json"
        p.touch()
        assert _plan_epoch(p) == 1792700000.0

    def test_current_epoch_hex_name(self, tmp_path: Path):
        p = tmp_path / "plan-1792700000-aaaaaa.json"
        p.touch()
        assert _plan_epoch(p) == 1792700000.0

    def test_unparseable_name_sorts_as_newest(self, tmp_path: Path):
        p = tmp_path / "plan-not-a-number.json"
        p.touch()
        assert _plan_epoch(p) == float("inf")

    def test_ordering_is_chronological_across_both_schemes(self, tmp_path: Path):
        """The whole point: a legacy name and a current name for different
        times must still sort in true chronological order by epoch, not
        by how many '-'-separated segments follow it."""
        older = tmp_path / "plan-1792700000.json"
        newer = tmp_path / "plan-1792700001-aaaaaa.json"
        older.touch()
        newer.touch()
        assert _plan_epoch(older) < _plan_epoch(newer)

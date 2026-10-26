"""Tests for plan-vs-disk diff (SPEC §10).

Verifies create / update / unchanged classification and the
human-readable text output.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from finmeta.engine.diff import (
    annotate_items, annotate_status, diff_file, diff_plan, diff_to_text,
)

from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestDiffFile:
    def test_create(self, tmp_dir: Path):
        """File absent on disk -> create."""
        p = tmp_dir / "new.jpg"
        fw = FileWrite(
            target_path=str(p), kind="poster",
            source_ref="https://example.com/x.jpg",
            content_sha256=_sha(b"content"),
        )
        result = diff_file(fw)
        assert result.status == "create"

    def test_unchanged(self, tmp_dir: Path):
        """File present with same sha -> unchanged."""
        p = tmp_dir / "same.jpg"
        p.write_bytes(b"identical")
        fw = FileWrite(
            target_path=str(p), kind="poster",
            source_ref="https://example.com/x.jpg",
            content_sha256=_sha(b"identical"),
        )
        result = diff_file(fw)
        assert result.status == "unchanged"

    def test_update(self, tmp_dir: Path):
        """File present with different sha -> update."""
        p = tmp_dir / "diff.jpg"
        p.write_bytes(b"old content")
        fw = FileWrite(
            target_path=str(p), kind="poster",
            source_ref="https://example.com/x.jpg",
            content_sha256=_sha(b"new content"),
        )
        result = diff_file(fw)
        assert result.status == "update"

    def test_target_path_preserved(self, tmp_dir: Path):
        p = tmp_dir / "file.jpg"
        fw = FileWrite(
            target_path=str(p), kind="poster",
            source_ref="src", content_sha256=_sha(b"x"),
        )
        result = diff_file(fw)
        assert result.target_path == str(p)
        assert result.kind == "poster"
        assert result.source_ref == "src"


class TestDiffPlan:
    def _make_plan(self, tmp_dir: Path) -> WritePlan:
        """Build a plan with 3 files: one create, one update, one unchanged."""
        base = tmp_dir / "Movies" / "Test Movie"
        base.mkdir(parents=True, exist_ok=True)

        # Unchanged file
        (base / "poster.jpg").write_bytes(b"poster-bytes")
        # File that will be updated
        (base / "backdrop.jpg").write_bytes(b"old-backdrop")

        return WritePlan(
            items=[
                ItemPlan(
                    item_id="mov01", item_name="Test Movie",
                    media_type="movie", tmdb_id=123, matched_by="provider_id",
                    files=[
                        FileWrite(
                            target_path=str(base / "movie.nfo"),
                            kind="metadata", source_ref="generated",
                            content_sha256=_sha(b"<movie/>"),
                        ),
                        FileWrite(
                            target_path=str(base / "poster.jpg"),
                            kind="poster", source_ref="local",
                            content_sha256=_sha(b"poster-bytes"),
                        ),
                        FileWrite(
                            target_path=str(base / "backdrop.jpg"),
                            kind="backdrop", source_ref="local",
                            content_sha256=_sha(b"new-backdrop"),
                        ),
                    ],
                ),
            ],
        )

    def test_counts(self, tmp_dir: Path):
        plan = self._make_plan(tmp_dir)
        entries = diff_plan(plan)
        assert len(entries) == 3
        statuses = {e.status for e in entries}
        assert statuses == {"create", "update", "unchanged"}

    def test_specific_statuses(self, tmp_dir: Path):
        plan = self._make_plan(tmp_dir)
        entries = diff_plan(plan)
        by_path = {e.target_path: e.status for e in entries}
        assert by_path[str(tmp_dir / "Movies/Test Movie/movie.nfo")] == "create"
        assert by_path[str(tmp_dir / "Movies/Test Movie/poster.jpg")] == "unchanged"
        assert by_path[str(tmp_dir / "Movies/Test Movie/backdrop.jpg")] == "update"

    def test_to_text_summary(self, tmp_dir: Path):
        plan = self._make_plan(tmp_dir)
        text = diff_to_text(plan)
        assert "1 create" in text
        assert "1 update" in text
        assert "1 unchanged" in text
        assert "Test Movie" in text
        assert "+ create" in text
        assert "~ update" in text
        assert "= unchanged" in text

    def test_to_text_unmatched(self, tmp_dir: Path):
        from finmeta.engine.plan import UnmatchedItem
        plan = WritePlan(
            items=[],
            unmatched=[UnmatchedItem(
                item_id="x", name="Mystery", reason="no match",
            )],
        )
        text = diff_to_text(plan)
        assert "Unmatched items" in text
        assert "Mystery" in text
        assert "no match" in text

    def test_empty_plan(self, tmp_dir: Path):
        plan = WritePlan(items=[])
        text = diff_to_text(plan)
        assert "0 create" in text
        assert "0 update" in text
        assert "0 unchanged" in text


class TestAnnotateStatus:
    """annotate_status persists create/update/unchanged onto the plan (SPEC §10)."""

    def _plan(self, tmp_dir: Path) -> WritePlan:
        base = tmp_dir / "Movies" / "Test Movie"
        base.mkdir(parents=True, exist_ok=True)
        (base / "poster.jpg").write_bytes(b"poster-bytes")   # matches  -> unchanged
        (base / "backdrop.jpg").write_bytes(b"old-backdrop")  # differs  -> update
        # movie.nfo absent on disk                              -> create
        return WritePlan(
            items=[
                ItemPlan(
                    item_id="mov01", item_name="Test Movie",
                    media_type="movie", tmdb_id=123, matched_by="provider_id",
                    files=[
                        FileWrite(target_path=str(base / "movie.nfo"), kind="metadata",
                                  source_ref="generated", content_sha256=_sha(b"<nfo/>")),
                        FileWrite(target_path=str(base / "poster.jpg"), kind="poster",
                                  source_ref="local", content_sha256=_sha(b"poster-bytes")),
                        FileWrite(target_path=str(base / "backdrop.jpg"), kind="backdrop",
                                  source_ref="local", content_sha256=_sha(b"new-backdrop")),
                    ],
                ),
            ],
        )

    def test_statuses_set_and_plan_returned(self, tmp_dir: Path):
        plan = self._plan(tmp_dir)
        # Before annotation every FileWrite is at the default "create".
        assert all(fw.status == "create" for fw in plan.items[0].files)
        ret = annotate_status(plan)
        assert ret is plan  # returns the same (mutated) plan
        assert [fw.status for fw in plan.items[0].files] == ["create", "unchanged", "update"]

    def test_empty_plan(self, tmp_dir: Path):
        plan = WritePlan(items=[])
        annotate_status(plan)
        assert plan.items == []

    def test_annotate_items_subset(self, tmp_dir: Path):
        """annotate_items stamps only the items it is given."""
        base = tmp_dir / "M"
        base.mkdir(parents=True, exist_ok=True)
        (base / "a.jpg").write_bytes(b"a")
        fw_unchanged = FileWrite(target_path=str(base / "a.jpg"), kind="poster",
                                 source_ref="local", content_sha256=_sha(b"a"))
        fw_create = FileWrite(target_path=str(base / "b.jpg"), kind="poster",
                              source_ref="local", content_sha256=_sha(b"b"))
        item = ItemPlan(item_id="i1", item_name="I", media_type="movie",
                        tmdb_id=1, matched_by="provider_id",
                        files=[fw_unchanged, fw_create])
        annotate_items([item])
        assert fw_unchanged.status == "unchanged"
        assert fw_create.status == "create"

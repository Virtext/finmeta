"""Golden-file tests for WritePlan JSON serialization (SPEC §10).

Verifies stable JSON ordering and round-trip deserialization.
"""
from __future__ import annotations

from pathlib import Path


from finmeta.engine.plan import (
    FileWrite, ItemPlan, UnmatchedItem, WritePlan,
)

GOLDEN = Path(__file__).parent / "golden" / "plan"


def _build_test_plan() -> WritePlan:
    """Build the canonical test plan (same as golden file)."""
    return WritePlan(
        items=[
            ItemPlan(
                item_id="mov001",
                item_name="The Matrix (1999)",
                media_type="movie",
                tmdb_id=603,
                matched_by="provider_id",
                files=[
                    FileWrite(
                        target_path="/work/fixture/library/Movies/The Matrix (1999)/movie.nfo",
                        kind="metadata",
                        source_ref="generated",
                        content_sha256="a" * 64,
                        status="create",
                    ),
                    FileWrite(
                        target_path="/work/fixture/library/Movies/The Matrix (1999)/poster.jpg",
                        kind="poster",
                        source_ref="/work/fixture/assets/603/poster.jpg",
                        content_sha256="b" * 64,
                        status="create",
                    ),
                    FileWrite(
                        target_path="/work/fixture/library/Movies/The Matrix (1999)/backdrop.jpg",
                        kind="backdrop",
                        source_ref="/work/fixture/assets/603/backdrop.jpg",
                        content_sha256="c" * 64,
                        status="create",
                    ),
                ],
            ),
            ItemPlan(
                item_id="mov002",
                item_name="Pulp Fiction (1994)",
                media_type="movie",
                tmdb_id=680,
                matched_by="title_year",
                files=[
                    FileWrite(
                        target_path="/work/fixture/library/Movies/Pulp Fiction (1994)/movie.nfo",
                        kind="metadata",
                        source_ref="generated",
                        content_sha256="d" * 64,
                        status="update",
                    ),
                    FileWrite(
                        target_path="/work/fixture/library/Movies/Pulp Fiction (1994)/poster.jpg",
                        kind="poster",
                        source_ref="https://image.tmdb.org/t/p/original/x.jpg",
                        content_sha256="e" * 64,
                        status="unchanged",
                    ),
                ],
            ),
            ItemPlan(
                item_id="tv001",
                item_name="Breaking Bad",
                media_type="tvshow",
                tmdb_id=1396,
                matched_by="provider_id",
                files=[
                    FileWrite(
                        target_path="/work/fixture/library/Shows/Breaking Bad/tvshow.nfo",
                        kind="metadata",
                        source_ref="generated",
                        content_sha256="f" * 64,
                        status="create",
                    ),
                    FileWrite(
                        target_path="/work/fixture/library/Shows/Breaking Bad/poster.jpg",
                        kind="poster",
                        source_ref="/work/fixture/assets/1396/poster.jpg",
                        content_sha256="0" * 64,
                        status="create",
                    ),
                    FileWrite(
                        target_path="/work/fixture/library/Shows/Breaking Bad/theme.mp3",
                        kind="theme_song",
                        source_ref="/work/fixture/assets/1396/theme.mp3",
                        content_sha256="1" * 64,
                        status="update",
                    ),
                ],
            ),
        ],
        unmatched=[
            UnmatchedItem(
                item_id="mov999",
                name="Unknown Film (2020)",
                reason='no TMDb match for "Unknown Film (2020)" (type=Movie)',
            ),
        ],
    )


class TestPlanJson:
    def test_golden_file(self):
        plan = _build_test_plan()
        actual = plan.to_json()
        golden = (GOLDEN / "full_plan.json").read_text()
        assert actual + "\n" == golden, (
            "Plan JSON mismatch:\n"
            f"--- expected ---\n{golden}\n"
            f"--- actual ---\n{actual}"
        )

    def test_stable_ordering(self):
        """Serializing the same plan twice produces identical JSON."""
        plan = _build_test_plan()
        j1 = plan.to_json()
        j2 = plan.to_json()
        assert j1 == j2

    def test_round_trip(self):
        """Deserializing the JSON produces an equivalent plan."""
        plan = _build_test_plan()
        json_str = plan.to_json()
        restored = WritePlan.from_json(json_str)
        assert restored == plan

    def test_item_count(self):
        plan = _build_test_plan()
        assert len(plan.items) == 3

    def test_file_count(self):
        plan = _build_test_plan()
        total_files = sum(len(item.files) for item in plan.items)
        assert total_files == 8

    def test_unmatched_count(self):
        plan = _build_test_plan()
        assert len(plan.unmatched) == 1
        assert plan.unmatched[0].name == "Unknown Film (2020)"

    def test_status_values(self):
        plan = _build_test_plan()
        statuses = [fw.status for item in plan.items for fw in item.files]
        assert "create" in statuses
        assert "update" in statuses
        assert "unchanged" in statuses

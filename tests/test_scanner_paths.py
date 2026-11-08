"""SPEC_S10 SC7: items whose Jellyfin path has no path_mappings entry."""
from __future__ import annotations

from finmeta.engine.scanner import (
    artwork_base_dir, check_artwork, check_path_mapping, partition_by_path,
)

from _scan_helpers import mapper_for, movie


def test_unmapped_items_are_partitioned_out(tmp_path):
    ok = movie("1", "Alpha")
    bad = movie("2", "Beta", path="/elsewhere/Movies/Beta/Beta.mkv")
    mapped, unmapped = partition_by_path([ok, bad], mapper_for(tmp_path))
    assert [i.id for i, _ in mapped] == ["1"]
    assert [i.id for i in unmapped] == ["2"]
    assert artwork_base_dir(bad, mapper_for(tmp_path)) is None


def test_unmapped_items_never_produce_artwork_findings(tmp_path):
    """Whether their poster exists is unknowable -- reporting it would be a lie."""
    bad = movie("2", "Beta", path="/elsewhere/Movies/Beta/Beta.mkv")
    mapped, unmapped = partition_by_path([bad], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies", kinds=["poster"]) == []
    assert [f.check_code for f in check_path_mapping(unmapped, library="Movies")] == [
        "path_unmapped"]


def test_one_finding_per_prefix_not_per_item(tmp_path):
    items = [movie(str(i), f"M{i}", path=f"/elsewhere/Movies/M{i}/M{i}.mkv")
             for i in range(50)]
    items.append(movie("x", "X", path="/other/Film/X/X.mkv"))
    _, unmapped = partition_by_path(items, mapper_for(tmp_path))
    out = check_path_mapping(unmapped, library="Movies")
    assert sorted(f.detail["prefix"] for f in out) == ["/elsewhere/Movies", "/other/Film"]
    big = next(f for f in out if f.detail["prefix"] == "/elsewhere/Movies")
    assert big.detail["count"] == 50
    assert len(big.detail["samples"]) == 3
    assert big.severity == "error"
    assert big.fix_kind == "external"
    assert big.item_id is None          # a config problem, not an item problem


def test_item_without_a_path_is_grouped_separately(tmp_path):
    _, unmapped = partition_by_path([movie("1", "Alpha", path="")], mapper_for(tmp_path))
    out = check_path_mapping(unmapped, library="Movies")
    assert [f.detail["prefix"] for f in out] == ["(no path)"]


def test_no_unmapped_items_means_no_findings():
    assert check_path_mapping([], library="Movies") == []

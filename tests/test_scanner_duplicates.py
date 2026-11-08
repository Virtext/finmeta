"""SPEC_S10 SC4: duplicate items, shared folders, multi-version items."""
from __future__ import annotations

from finmeta.engine.scanner import DuplicateIndex, partition_by_path

from _scan_helpers import mapper_for, movie, series


def _index(items, tmp_path, *, library="Movies") -> DuplicateIndex:
    dup = DuplicateIndex(library=library)
    mapped, unmapped = partition_by_path(items, mapper_for(tmp_path))
    for item, base in mapped:
        dup.add(item, base)
    for item in unmapped:
        dup.add(item, None)
    return dup


def test_two_items_with_the_same_tmdb_id_are_one_group_finding(tmp_path):
    items = [
        movie("1", "Alpha", provider_ids={"Tmdb": "603"}),
        movie("2", "Alpha (2)", provider_ids={"Tmdb": "603"}),
    ]
    out = _index(items, tmp_path).findings()
    assert [f.check_code for f in out] == ["duplicates.same_provider_id"]
    f = out[0]
    assert f.severity == "warning"
    # never delete or move media: there is no fix to offer
    assert f.fix_kind == "none"
    assert f.detail["provider"] == "Tmdb"
    assert f.detail["provider_id"] == "603"
    assert f.detail["count"] == 2
    assert [m["item_id"] for m in f.detail["members"]] == ["1", "2"]


def test_a_unique_provider_id_is_not_a_duplicate(tmp_path):
    items = [movie("1", "Alpha"), movie("2", "Beta"), series("s1", "Gamma")]
    assert _index(items, tmp_path).findings() == []


def test_a_group_matched_by_two_providers_is_reported_once(tmp_path):
    items = [
        movie("1", "Alpha", provider_ids={"Tmdb": "603", "Imdb": "tt0133093"}),
        movie("2", "Alpha copy", provider_ids={"Tmdb": "603", "Imdb": "tt0133093"}),
    ]
    out = [f for f in _index(items, tmp_path).findings()
           if f.check_code == "duplicates.same_provider_id"]
    assert len(out) == 1


def test_items_without_provider_ids_are_never_grouped(tmp_path):
    items = [movie("1", "Alpha", provider_ids={}), movie("2", "Beta", provider_ids={})]
    assert _index(items, tmp_path).findings() == []


def test_two_movies_in_one_folder(tmp_path):
    items = [
        movie("1", "Alpha", path="/media/Movies/Shared/a.mkv", provider_ids={"Tmdb": "1"}),
        movie("2", "Beta", path="/media/Movies/Shared/b.mkv", provider_ids={"Tmdb": "2"}),
    ]
    out = _index(items, tmp_path).findings()
    assert [f.check_code for f in out] == ["duplicates.same_folder"]
    assert out[0].detail["count"] == 2
    assert out[0].detail["folder"].endswith("Movies/Shared")
    assert out[0].fix_kind == "none"


def test_series_sharing_a_root_is_not_a_folder_duplicate(tmp_path):
    """Every episode shares its show folder by design; two series never do."""
    items = [series("s1", "Gamma"), series("s2", "Delta")]
    assert _index(items, tmp_path, library="TV").findings() == []


def test_multi_version_item_is_info_only(tmp_path):
    items = [movie("1", "Alpha", media_source_count=2)]
    out = _index(items, tmp_path).findings()
    assert [f.check_code for f in out] == ["duplicates.multi_version"]
    assert out[0].severity == "info"
    assert out[0].fix_kind == "none"
    assert out[0].detail["versions"] == 2
    assert [f.check_code for f in _index([movie("1", "A", media_source_count=1)],
                                         tmp_path).findings()] == []


def test_member_list_is_capped(tmp_path):
    items = [movie(str(i), f"M{i}", provider_ids={"Tmdb": "603"}) for i in range(14)]
    out = _index(items, tmp_path).findings()
    f = next(f for f in out if f.check_code == "duplicates.same_provider_id")
    assert f.detail["count"] == 14
    assert len(f.detail["members"]) == 10
    assert f.detail["members_truncated"] == 4


def test_duplicates_are_found_across_pages(tmp_path):
    """The index accumulates, so a pair split over two pages still groups."""
    dup = DuplicateIndex(library="Movies")
    first = movie("1", "Alpha", provider_ids={"Tmdb": "603"})
    second = movie("2", "Alpha copy", provider_ids={"Tmdb": "603"})
    for page in ([first], [second]):
        mapped, _ = partition_by_path(page, mapper_for(tmp_path))
        for item, base in mapped:
            dup.add(item, base)
    assert [f.check_code for f in dup.findings()] == ["duplicates.same_provider_id"]

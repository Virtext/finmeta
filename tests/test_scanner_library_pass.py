"""SPEC_S10 §A/§H: the tier-1 library pass is bounded, sorted, and provider-free."""
from __future__ import annotations


from finmeta.engine.scanner import SCAN_ITEM_FIELDS, scan_library

from _scan_helpers import FakeJellyfin, ExplodingSource, mapper_for, movie, movie_library


def _run(jf, tmp_path, lib_cfg=None, **kw):
    emitted = []
    res = scan_library(
        jf, library="Movies", lib_cfg=lib_cfg or movie_library(),
        mapper=mapper_for(tmp_path), emit=emitted.append, **kw)
    return res, [f for batch in emitted for f in batch]


def test_pass_is_paged_type_filtered_and_sorted(tmp_path):
    jf = FakeJellyfin([movie(str(i), f"M{i:03d}") for i in range(45)])
    res, findings = _run(jf, tmp_path, page_size=20)
    assert res.items_scanned == 45
    assert [c["limit"] for c in jf.calls] == [20, 20, 20]
    assert [c["start_index"] for c in jf.calls] == [0, 20, 40]
    for call in jf.calls:
        assert call["include_item_types"] == ["Movie", "Series"]
        assert call["fields"] == SCAN_ITEM_FIELDS
        assert call["sort_by"] == ["SortName"]      # stable page boundaries
        assert call["parent_id"] == jf.library_id
    # every item is missing its poster, so one finding each
    assert len(findings) == 45
    assert res.findings == 45


def test_findings_are_flushed_per_page_not_at_the_end(tmp_path):
    jf = FakeJellyfin([movie(str(i), f"M{i:03d}") for i in range(30)])
    batches = []
    scan_library(jf, library="Movies", lib_cfg=movie_library(),
                 mapper=mapper_for(tmp_path), emit=batches.append, page_size=10)
    # 3 artwork batches (one per page) + 1 tail batch is not produced here
    # (no duplicates, no unmapped paths), so exactly 3
    assert len(batches) == 3
    assert all(len(b) == 10 for b in batches)


def test_progress_is_reported_per_page(tmp_path):
    jf = FakeJellyfin([movie(str(i), f"M{i:03d}") for i in range(25)])
    seen = []
    scan_library(jf, library="Movies", lib_cfg=movie_library(),
                 mapper=mapper_for(tmp_path), emit=lambda f: None,
                 page_size=10, progress_cb=lambda done, total: seen.append((done, total)))
    assert seen == [(10, 25), (20, 25), (25, 25)]


def test_tier1_issues_no_provider_traffic(tmp_path):
    """Any TMDb/fanart/Sonarr call in tier 1 is a bug, not a slow path.

    Media streams ARE fetched in tier 1 -- but only through the batched
    endpoint (FakeJellyfin.get_media_streams raises to pin that): one
    request per ~50 items is a tier-1 price, one per item is not.
    """
    jf = FakeJellyfin([movie("1", "Alpha")])
    jf.tmdb = ExplodingSource("tmdb")          # would raise if touched
    jf.fanart = ExplodingSource("fanart")
    jf.sonarr = ExplodingSource("sonarr")
    res, findings = _run(jf, tmp_path)
    assert res.items_scanned == 1
    assert [f.check_code for f in findings] == ["artwork.missing.poster"]
    assert jf.stream_calls == [["1"]]


def test_empty_library_is_not_an_error(tmp_path):
    res, findings = _run(FakeJellyfin([]), tmp_path)
    assert (res.items_scanned, res.findings, findings) == (0, 0, [])
    assert res.skipped_reason is None


def test_library_missing_in_jellyfin_is_skipped_with_a_reason(tmp_path):
    jf = FakeJellyfin([movie("1", "Alpha")], exists=False)
    res, findings = _run(jf, tmp_path)
    assert res.skipped_reason == "library not found in Jellyfin"
    assert (res.items_scanned, findings) == (0, [])


def test_the_tail_carries_duplicates_and_unmapped_paths(tmp_path):
    jf = FakeJellyfin([
        movie("1", "Alpha", provider_ids={"Tmdb": "603"}),
        movie("2", "Alpha copy", provider_ids={"Tmdb": "603"}),
        movie("3", "Beta", path="/elsewhere/Beta/Beta.mkv"),
    ])
    res, findings = _run(jf, tmp_path, page_size=2)
    codes = sorted(f.check_code for f in findings)
    assert codes == [
        "artwork.missing.poster", "artwork.missing.poster",
        "duplicates.same_provider_id", "path_unmapped",
    ]
    assert res.findings == len(findings)
    # the unmapped item got no artwork finding, the two mapped ones did
    assert sum(1 for f in findings if f.check_code == "artwork.missing.poster") == 2

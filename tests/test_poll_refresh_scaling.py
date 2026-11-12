"""engine.executor._poll_refresh scaling (audit 2026-11-11 "C2").

Before this fix, every 2s round re-called jf.get_item for EVERY item in
item_ids -- including items that had already settled on an earlier round --
and never broke out of a round's loop on the first still-unsettled id. A
library-wide apply touching 1000 posters could issue up to
(timeout / poll_interval) x 1000 get_item calls to produce one boolean.

The fix drops a settled id from the working set the moment it's confirmed
(never re-checked again) and stops checking an image round's remaining ids
the moment one is found unsettled (that round has already failed; nothing
is learned from checking the rest), and skips the theme-song loop entirely
for any round where images are still pending.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, Optional, Sequence

from finmeta.engine.executor import _poll_refresh


class _CountingJF:
    """Fake Jellyfin client: an id "settles" once it has been checked
    ``settle_after`` times (default 1, i.e. settled on the very first
    check)."""

    def __init__(
        self,
        image_settle_after: Optional[Dict[str, int]] = None,
        song_settle_after: Optional[Dict[str, int]] = None,
        raise_for_images: Sequence[str] = (),
    ):
        self.image_settle_after = image_settle_after or {}
        self.song_settle_after = song_settle_after or {}
        self.raise_for_images = set(raise_for_images)
        self.get_item_calls: list[str] = []
        self.get_theme_songs_calls: list[str] = []
        self._image_checks: Dict[str, int] = {}
        self._song_checks: Dict[str, int] = {}

    def get_item(self, item_id):
        self.get_item_calls.append(item_id)
        if item_id in self.raise_for_images:
            raise RuntimeError("boom")
        self._image_checks[item_id] = self._image_checks.get(item_id, 0) + 1
        needed = self.image_settle_after.get(item_id, 1)
        tags = {"Primary": "new"} if self._image_checks[item_id] >= needed else {}
        return SimpleNamespace(image_tags=tags)

    def get_theme_songs(self, item_id):
        self.get_theme_songs_calls.append(item_id)
        self._song_checks[item_id] = self._song_checks.get(item_id, 0) + 1
        needed = self.song_settle_after.get(item_id, 1)
        return ["song.mp3"] if self._song_checks[item_id] >= needed else []


class TestSettledIdsAreDroppedNotRechecked:
    def test_settled_item_checked_exactly_once(self):
        """A takes 1 check to settle, B takes 4. Once A settles it must
        never be checked again -- regardless of how many more rounds B
        needs, A's total call count stays at exactly 1."""
        jf = _CountingJF(image_settle_after={"A": 1, "B": 4})
        ok = _poll_refresh(
            jf, ["A", "B"], [], poll_interval=0.01, timeout=5.0,
            pre_tags={"A": {}, "B": {}},
        )
        assert ok is True
        assert jf.get_item_calls.count("A") == 1
        assert jf.get_item_calls.count("B") == 4


class TestBreaksAtFirstUnsettledId:
    def test_later_ids_in_the_round_are_not_checked(self):
        """Insertion order A, B, C. B needs 5 checks; A and C settle
        immediately (1 check). Every round A fails -- wait, A settles
        round 1 -- then B blocks every round until round 5, at which point
        the loop (having NOT broken on B's success) continues on to C in
        the SAME round. C is therefore checked exactly once, on round 5 --
        if the old "check every id every round" behaviour were still
        present, C would have been checked on every one of the 5 rounds."""
        jf = _CountingJF(image_settle_after={"A": 1, "B": 5, "C": 1})
        ok = _poll_refresh(
            jf, ["A", "B", "C"], [], poll_interval=0.01, timeout=5.0,
            pre_tags={"A": {}, "B": {}, "C": {}},
        )
        assert ok is True
        assert jf.get_item_calls.count("A") == 1
        assert jf.get_item_calls.count("B") == 5
        assert jf.get_item_calls.count("C") == 1

    def test_exception_also_breaks_the_round(self):
        jf = _CountingJF(
            image_settle_after={"A": 1, "C": 1}, raise_for_images=["B"],
        )
        ok = _poll_refresh(
            jf, ["A", "B", "C"], [], poll_interval=0.01, timeout=0.05,
            pre_tags={"A": {}, "B": {}, "C": {}},
        )
        assert ok is False  # B never settles -- times out
        # C is never reached: B (which always raises) blocks every round.
        assert jf.get_item_calls.count("C") == 0


class TestThemeSongLoopSkippedWhileImagesPending:
    def test_theme_songs_not_polled_until_all_images_settled(self):
        """A needs 2 checks; T (a theme song) would settle on its very
        first check. Without the fix, get_theme_songs would be called on
        round 1 too (and find T already settled) -- the fix must not call
        it at all until the image loop empties out."""
        jf = _CountingJF(
            image_settle_after={"A": 2}, song_settle_after={"T": 1},
        )
        ok = _poll_refresh(
            jf, ["A"], ["T"], poll_interval=0.01, timeout=5.0,
            pre_tags={"A": {}},
        )
        assert ok is True
        assert jf.get_item_calls.count("A") == 2
        assert jf.get_theme_songs_calls.count("T") == 1


class TestBasicSemanticsUnchanged:
    def test_empty_ids_returns_true_immediately(self):
        jf = _CountingJF()
        assert _poll_refresh(jf, [], [], poll_interval=0.01, timeout=1.0) is True
        assert jf.get_item_calls == []
        assert jf.get_theme_songs_calls == []

    def test_timeout_returns_false(self):
        jf = _CountingJF(image_settle_after={"A": 10_000})
        ok = _poll_refresh(
            jf, ["A"], [], poll_interval=0.01, timeout=0.05,
            pre_tags={"A": {}},
        )
        assert ok is False

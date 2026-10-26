"""S9d4c: overlays must never stack across repeated applies (O7).

Operator-reported, confirmed real: after changing overlay settings and
re-applying more than once, OLD badges showed up composited underneath NEW
ones. Root cause: engine.runs.find_latest_backup (now find_original_backup)
returned the MOST RECENT backup of a target path -- but every apply that
overwrites an existing file backs up whatever is CURRENTLY on disk first,
so after the FIRST apply, every LATER backup is of Finmeta's own prior
(already-overlaid) output, not the true pre-Finmeta original. Using the
newest backup as the overlay's "original" meant run 3 composited its badges
on top of run 2's output, which was itself composited on top of run 1's.

This test reproduces the exact chain (3 applies, each writing to the same
target, each triggering a NEW backup of the previous state) and asserts
every single apply renders from the TRUE original -- never from a prior
Finmeta-authored, already-overlaid state.
"""
from __future__ import annotations

import hashlib

from PIL import Image
import io

from finmeta.engine.overlay import TextBadge, overlay_file_write
from finmeta.engine.runs import backup
from finmeta.config.models import OverlayStyle

STYLE = OverlayStyle(position="bottom_left", margin=10, font_size=20,
                     bg="#000000B0", fg="#FFFFFF", radius=4)


def _poster(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (200, 300), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_three_successive_applies_all_composite_from_the_true_original(tmp_path):
    config_dir = tmp_path / "config"
    target = tmp_path / "media" / "poster.jpg"
    target.parent.mkdir(parents=True)

    true_original = _poster((10, 20, 30))
    target.write_bytes(true_original)
    original_sha = hashlib.sha256(true_original).hexdigest()

    # Apply 1: plan-build (overlay_file_write) happens BEFORE that apply's
    # own backup+write (matches the real order: plan, then apply/executor).
    # No backup exists yet -- renders straight from the on-disk original.
    fw1 = overlay_file_write(
        target_path=target, candidate_bytes=true_original, candidate_ref=str(target),
        badges=[TextBadge(text="v1")], style=STYLE, config_dir=config_dir,
    )
    # Apply 1 executes: backs up the pre-write (true original) state, then
    # overwrites with its own (here, stand-in) output.
    backup("run1", target, config_dir=config_dir)
    target.write_bytes(b"RUN1-OUTPUT-PLACEHOLDER")  # stand-in for the real overlaid bytes

    # Apply 2: a DIFFERENT overlay setting (different badge). One backup
    # exists (run1 = the true original) -- must render from THAT.
    fw2 = overlay_file_write(
        target_path=target, candidate_bytes=true_original, candidate_ref=str(target),
        badges=[TextBadge(text="v2")], style=STYLE, config_dir=config_dir,
    )
    # Apply 2 executes: backs up what's on disk right NOW -- run 1's own
    # output -- then overwrites with its own output. This must NOT become
    # the "original" for apply 3.
    backup("run2", target, config_dir=config_dir)
    target.write_bytes(b"RUN2-OUTPUT-PLACEHOLDER")

    # Apply 3: two backups exist now (run1 = true original, run2 = run1's
    # already-overlaid output). Must still pick run1, not the newer run2.
    fw3 = overlay_file_write(
        target_path=target, candidate_bytes=true_original, candidate_ref=str(target),
        badges=[TextBadge(text="v3")], style=STYLE, config_dir=config_dir,
    )

    # Every single one of the three renders must have been built from the
    # TRUE original bytes (same source_ref pointing at the run1 backup of
    # the true original, once one exists) -- never from "RUN1-OUTPUT..." or
    # "RUN2-OUTPUT...". We can't inspect source bytes directly through the
    # FileWrite, but source_ref tells us which backup (if any) was used.
    assert fw1.source_ref == str(target)  # no backup existed yet at apply 1's render time
    original_backup_path = config_dir / "backups" / "run1" / "media" / "poster.jpg"
    assert fw2.source_ref == str(original_backup_path)
    assert fw3.source_ref == str(original_backup_path)  # NOT run2's backup
    # And that backup file is still exactly the true original's bytes.
    assert hashlib.sha256(original_backup_path.read_bytes()).hexdigest() == original_sha


def test_a_manual_candidate_override_renders_from_the_new_candidate_not_a_stale_backup(tmp_path):
    """2026-10-20, operator-reported ("picking a new poster candidate does
    nothing"): once a backup exists (this item was applied before), every
    later render -- override or not -- used to always fall back to that
    backup, silently discarding whatever NEW candidate the operator had just
    picked. is_override=True must make the fresh candidate_bytes win instead,
    regardless of what's backed up."""
    config_dir = tmp_path / "config"
    target = tmp_path / "media" / "poster.jpg"
    target.parent.mkdir(parents=True)

    original = _poster((10, 20, 30))
    target.write_bytes(original)

    # Apply 1 (auto-picked candidate): backs up the true original.
    overlay_file_write(
        target_path=target, candidate_bytes=original, candidate_ref="auto-candidate",
        badges=[TextBadge(text="v1")], style=STYLE, config_dir=config_dir,
    )
    backup("run1", target, config_dir=config_dir)
    target.write_bytes(b"RUN1-OUTPUT-PLACEHOLDER")

    # Operator now manually picks a DIFFERENT poster candidate.
    new_candidate = _poster((200, 100, 50))
    fw = overlay_file_write(
        target_path=target, candidate_bytes=new_candidate, candidate_ref="https://example/new.jpg",
        badges=[TextBadge(text="v2")], style=STYLE, config_dir=config_dir,
        is_override=True,
    )

    assert fw.source_ref == "https://example/new.jpg"   # the NEW candidate, not the run1 backup
    assert fw.meta["lock_sha256"] == hashlib.sha256(new_candidate).hexdigest()

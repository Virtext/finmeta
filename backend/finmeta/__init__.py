"""Finmeta: a Kometa-equivalent for Jellyfin."""

__version__ = "1.0.0"


def _harden_pillow() -> None:
    """Cap Pillow's decompression-bomb limit (CLAUDE.md §5).

    Every poster/backdrop/logo Finmeta decodes is untrusted input: it comes
    from TMDb, fanart.tv, an imported third-party Kometa pack, or a file
    dropped in the asset dir. Pillow's own default (~89 Mpx) only *warns*,
    and raises only past twice that, so a hostile image can still make the
    process allocate gigabytes before anything stops it. 40 Mpx is far above
    any real poster or backdrop (a 4K backdrop is ~8.3 Mpx) and turns that
    class of input into a clean exception instead.

    Set in the package root so it applies to every entry point -- API server,
    CLI, tests -- instead of depending on each Image.open() call site.
    """
    try:
        from PIL import Image
    except Exception:       # pragma: no cover - Pillow is a hard dependency
        return
    Image.MAX_IMAGE_PIXELS = 40_000_000


_harden_pillow()

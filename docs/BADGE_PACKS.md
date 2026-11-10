# Badge packs (image packs)

How Finmeta's overlay badges (resolution, HDR/dynamic range, audio codec, …)
get their artwork. This is the **only** supported way to add badge images —
the automatic "import a Kometa overlay config" feature was removed
2026-10-31 (see `docs/HISTORY.md`): even Kometa's own built-in defaults don't
survive the translation reliably, so there is no generic importer any more.
Packs always arrive by hand, built to the schema below.

This system (`engine/image_packs.py`, SPEC S9 D3) is separate from the older
Kometa **rating-logo** pack (`engine/logo_packs/kometa.yml`, SPEC S9c8) —
that one only ever covers the five rating-service logos (IMDb/TMDb/
Metacritic/Letterboxd/Trakt) plus Rotten Tomatoes' fresh/rotten icons, fetched
on demand because they're real trademarks. Everything in this document is
about resolution/HDR/audio-codec badges, which have no such restriction.

Every `<config>` below means **the folder you mounted as Finmeta's config
volume in your `docker run`/compose file** (commonly `/config` inside the
container) — never a folder named `assets` or `config` inside the Finmeta
project/image itself. The Packs page in the app shows the exact resolved
absolute path for your install if you're unsure.

## Quick start: adding your own badge images

### The simple way: one flat folder, no pack.yml

For the common case (override resolution/HDR/audio badges) you don't need
to write a pack.yml or sort images into per-category subfolders at all.
Make one folder and drop correctly-named PNGs straight into it:

```
<config>/assets/packs/my-pack/images/res_4k.png
<config>/assets/packs/my-pack/images/dv_hdr.png
<config>/assets/packs/my-pack/images/dtsx.png
```

That's it — no `pack.yml`, no `resolution/`/`dynamic_range`/`audio`
subfolders. Finmeta looks each filename up directly against K9's own key
vocabulary (the table below) and works out which category it belongs to —
every key is unique across all three categories, so the filename alone is
enough. Add only the keys you want to override; anything you don't provide
still falls back to whatever pack is next in the profile's list (or to plain
text). `GET /api/packs` (the "missing images" list in the UI) shows exactly
which of the *full* K9 key set you haven't added yet, same as any other pack.

This only works for plain `format_rule` matching (badges driven by Finmeta's
own stream/filename detection, not a custom regex). If you need
`filename_regex`/`metadata`/`rating_tier`/`format_rule_combo`, or just want
your own category names, write a `pack.yml` by hand (see "Layout" below) or
use "+ New pack" in the UI, which scaffolds one for you.

### Three things that commonly go wrong

These bit a real attempt (both with the flat method above and with a
hand-written pack.yml), so they're worth calling out explicitly:

1. **Wrong location.** Images go under `<config>/assets/packs/<your-pack-name>/
   images/...`, not anywhere else — not `fixture/assets/` (that's the test
   library's own media folder, never scanned for pack images) and not a
   folder you create yourself elsewhere.
2. **Wrong folder/category name**, if using the pack.yml method. The
   "+ New pack" checkboxes are labelled "Dynamic range (HDR)" for
   readability, but the actual folder underneath is named `dynamic_range`
   (all lowercase, underscore, no parentheses) — same idea for `resolution`
   and `audio`. The form shows the real folder name in parentheses next to
   each checkbox; that's the one to use, not the label text. (The flat
   method above sidesteps this entirely — there's no folder name to get
   wrong.)
3. **Wrong file format or file name.** Images must be **PNG** — Finmeta's
   renderer is PIL-based and never reads SVG, no matter where you put one.
   And the filename (minus `.png`) must be one of the exact keys your
   category's matcher expects — for `format_rule` that's K9's own
   vocabulary below; `res_4k.png` works, `4K_UHD.png` never will under that
   matcher.

If you'd rather match on the video filename itself (scene-release tags like
`2160p`, `DTS-X`, `Atmos`) instead of Finmeta's own stream-detected facts,
see the `filename_regex` matcher below — needs a hand-written pack.yml
(the flat no-yml method only ever does `format_rule`), and keys can be
anything you want since there's no fixed vocabulary to match.

## Layout

The pack.yml layout (needed for anything beyond the simple flat case above —
custom categories, filename_regex, metadata, rating_tier, format_rule_combo):

```
<config>/assets/packs/<pack-name>/
  pack.yml
  images/
    <category>/
      <key>.png
```

`pack.yml`:

```yaml
name: my-pack              # should match the directory name
description: Optional one-liner shown in the UI.
categories:
  resolution:
    matcher: format_rule
    keys: [res_4k, res_1080p, res_720p, res_576p, res_480p]
  dynamic_range:
    matcher: format_rule
    keys: [dv_hdr_plus, dv_hdr, dv, hdr10plus, hdr10, hlg]
  audio:
    matcher: format_rule
    keys: [truehd_atmos, ddplus_atmos, truehd, ddplus, dd, dtsx, dtshd_ma, dts, aac]
```

A category name is free text — it becomes its own overlay element. You don't
need all three categories above, and you can add others (`edition`,
`network`, …) with a different matcher (see below). `GET /api/packs` reports,
per pack: which images are present, which `keys` have no matching file yet
(`missing_images`, dotted `category/key` paths), and any `warnings` (see
"Image requirements").

## The `format_rule` matcher — use this unless you have a reason not to

Finmeta already detects resolution, dynamic range and audio codec from the
real media stream (K9 rules, `engine/format_rules.py`) — `format_rule` just
asks "what did K9 decide?" and uses that rule's own key. **No regex, no
filename guessing, nothing to get wrong.** The three category names above
(`resolution`, `dynamic_range`, `audio`) and their key lists are not
arbitrary — they're exactly K9's own vocabulary, so copy them verbatim. A
pack only needs the keys it actually wants to override; an absent key just
means "no badge" for that case (never a crash, never a fallback guess).

| category         | keys (first match wins, in this order)                                                  |
|-------------------|-------------------------------------------------------------------------------------------|
| `resolution`      | `res_4k`, `res_1080p`, `res_720p`, `res_576p`, `res_480p`                                  |
| `dynamic_range`   | `dv_hdr_plus`, `dv_hdr`, `dv`, `hdr10plus`, `hdr10`, `hlg`                                  |
| `audio`           | `truehd_atmos`, `ddplus_atmos`, `dtsx`, `dtshd_ma`, `truehd`, `ddplus`, `dts`, `dd`, `aac` |

(`dv_hdr`/`dv_hdr_plus` fall back to `dv`+`hdr10`/`hdr10plus` separately if
the combined key is missing from your pack — see `_COMBINE_PARTS` in
`engine/image_pack_matchers.py`.)

## Other matchers (advanced)

- `filename_regex` — one regex **per key** (`{key: pattern}`), tried in
  order (first match wins); matches against the item's filename. This is the
  shape a real Kometa/jmxd-style scene-release-tag pack uses when it keys off
  filename tags instead of the actual decoded stream. Two things worth
  knowing before writing these by hand:
  - **Order matters, most-specific-first.** If `hdr` (bare `\bHDR\b`) is
    declared before `hdr10_plus`, a filename containing `HDR10+` matches the
    generic `hdr` key first and the more specific badge never shows — same
    principle K9's own rules use (see `engine/format_rules.py`'s priority
    field).
  - **A tag made of two words needs an order-independent pattern**, because
    real scene names don't always put them in the order you'd guess — e.g.
    resolution almost always comes *before* source
    (`...2160p.WEB-DL...`, not `...WEB.2160p...`). A plain `WEB.*2160p`
    pattern silently never matches real files for exactly that reason. Use a
    lookahead instead: `(?=.*WEB)(?=.*2160p)` requires both substrings
    anywhere, in either order.
  - YAML quoting: use **single quotes** around a pattern that contains a
    backslash (`'\b1080p\b'`), not double quotes — `"\b1080p\b"` is invalid
    YAML (`\b`/`\.` aren't recognised double-quote escapes and the file
    fails to parse, not just fails to match).
- `metadata` — looks up a metadata field's value directly against your
  `keys` list.
- `rating_tier` — buckets a TMDb `vote_average` into tiers you define
  (`tiers: {gold: ">=8.0", ...}`).
- `format_rule_combo` — combines resolution + dynamic_range + audio K9
  results into **one** image per combination (`components: [resolution,
  dynamic_range, audio]`), for packs that ship a single badge like
  "4K Dolby Vision Atmos" instead of three separate ones.

## Image requirements

- **PNG, transparent background, tightly cropped to the visible content.**
  Finmeta always scales your image to a target *height* and keeps its aspect
  ratio (`overlay_render._logo_scaled_width`) — so any width works, but the
  source must already be cropped to its real content bounding box.
- **Don't ship a full poster-sized canvas.** This bit a real pack in
  practice (2026-10-27, see `docs/HISTORY.md`): an imported pack's images
  were 1000×1500 poster canvases with the actual badge pre-positioned in one
  corner and the rest transparent — Finmeta scaled the *whole* canvas down to
  badge height, so the visible badge shrank to a sliver. `GET /api/packs`
  now flags any image over 400px tall as a `warnings` entry for exactly this
  reason — treat that warning as "re-crop this file," not as noise.
- Recommended native size: roughly 150–250px tall (Finmeta's own bundled
  packs, below, render at ~200–250px). This gives clean downscaling to
  whatever `size` the overlay profile configures without a blurry upscale.

## Finmeta's own bundled packs

Two packs ship with Finmeta itself (seeded into `<config>/assets/packs/`
automatically on first start — see `engine.image_packs.seed_bundled_packs` —
then behave exactly like any hand-made pack: editable, deletable, never
re-seeded once deleted):

- **`finmeta_clean`** — flat off-white wordmarks, no chips, no gradients.
- **`finmeta_dynamic`** — the same wordmarks, but each badge family gets its
  own diagonal gradient (resolution: silver; Dolby Vision: indigo→cyan;
  HDR10/HDR10+/HLG: magenta→amber; Dolby-licensed audio: teal→violet;
  DTS-licensed audio: gold→orange).

Both are 100% original artwork (own typography, own icon shapes) — not a
copy of any third-party pack's pixels — so, unlike every other pack
mentioned in this document, they're safe to commit and ship in the Docker
image outright. The one deliberate exception: the small "double-D" glyph on
genuinely Dolby-licensed formats (Dolby Vision, Atmos, DD/DD+, TrueHD — never
DTS, a different vendor) is Finmeta's own redrawn take on that family mark,
used under the same self-hosted/non-commercial convention every Kometa- and
jmxd-style overlay pack already relies on.

The PNGs Finmeta actually renders live at `backend/finmeta/assets/bundled_packs/
<name>/` — copy either one as a starting point for your own pack. Editable
vector sources for the same 20 badges live at `docs/assets/badge_packs/<name>/
<category>/<key>.svg` (not read at runtime — Finmeta's renderer is PIL/PNG
only, no SVG support); re-export to PNG at your own size if you want to tweak
one.

## Activating a pack

Add its name to an overlay profile's `packs` list (`overlays.profiles.<name>.
packs`, in priority order — the first pack in the list that defines a
category wins for that category). `pack_overrides.<pack>.<category>.
enabled: false` turns off one category from one pack without removing the
whole pack. Both are edited from the Overlays tab in the UI, or directly in
`config.yml`.

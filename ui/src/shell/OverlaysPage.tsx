import React, { useCallback, useEffect, useRef, useState } from 'react';
import { dump as yamlDump } from 'js-yaml';
import {
  Badge, Button, Card, CardHeader, CardBody, Dialog, EmptyState,
  ErrorState, Input, PageHeader, Select, Skeleton, Tabs, useToast,
} from '../ui';
import {
  getConfig, getOverlays, putConfig, getLibraries, getItemsLight, previewOverlay, getHelp, saveOverlay,
} from '../api';
import type { OverlaysPreset, OverlaysUserStyle, OverlayPreviewEntry } from '../api';
import type { ItemLightView, HelpResp } from '../types';
import {
  ELEMENT_ORDER, ELEMENT_LABELS, BaseSettings, ElementControls, FormatRulesSection,
} from '../components/OverlaysEditor';
import type { DocElementState, OverlayDoc } from '../components/OverlaysEditor';

// SPEC_S14_UI.md phase P5 (the risk phase): "Studio chrome and preset
// gallery first, then drag/resize." That first pass shipped profile list,
// element on/off, a preset gallery, and the save path; a second pass added
// the live draggable canvas (item picker + drag-to-move/resize). This third
// pass (2026-11-09, operator-requested) brings the Studio to full parity
// with the classic Settings ▸ Overlays tab -- base settings (anchor/margin/
// font/colors/radius), per-element fine-tuning (color/back_color/radius/
// padding/opacity/full_width/ribbon text/status colors) and Format Rules --
// by REUSING OverlaysEditor.tsx's BaseSettings/ElementControls/
// FormatRulesSection components directly rather than re-implementing them
// (same reasoning as PacksPanel's extraction: these are already correct and
// already exercised by the classic tab). Reaching parity is the
// precondition the operator set before Settings' own Overlays tab can be
// hidden the way Libraries/Schedules/Trending/Collections just were.
//
// A fourth pass (still 2026-11-09) closes the one gap the operator flagged
// after reviewing the third: legacy's "Global (defaults)" scope, which
// edits NAMED REUSABLE STYLES (overlays.user_styles, PUT /api/overlays/
// {name}) -- NOT overlays.style itself, and NOT tied to any library. Added
// as a parallel "Named styles" list beside Profiles, sharing the same
// BaseSettings/ElementControls/FormatRulesSection/canvas editor body.
//

// draftStyle now carries EVERY element's full state (position/offset/size/
// color/etc. AND enabled), one entry per ELEMENT_ORDER key -- the earlier
// pass's separate draftElements Set is gone, unified into this single
// source of truth (matching OverlayDoc's own shape, and ElementControls'
// own checkbox which flips draftStyle[key].enabled directly).
//
// The save path is the exact thing tests/test_api.py's
// test_overlay_profile_elements_and_style_round_trip guards: read the full
// config, mutate ONLY overlays.profiles.<name>, write the full config back.
// The 2026-10-19 bug was a save button that wrote {style: doc} alone and
// silently dropped the profile's flat `elements` list -- this writes BOTH,
// derived from the same draftStyle so they can never disagree.

interface Profile {
  elements?: string[];
  style?: Record<string, unknown>;
  [k: string]: unknown;
}

function elementsFromStyle(style: unknown): string[] {
  const els = (style as any)?.elements;
  if (!els || typeof els !== 'object') return [];
  return Object.entries(els).filter(([, v]: [string, any]) => v?.enabled).map(([k]) => k);
}

type BaseDoc = Omit<OverlayDoc, 'elements'>;

const DEFAULT_BASE: BaseDoc = {
  base_position: 'top_left', margin: 20, font_size: 42, bg: '#000000B0', fg: '#FFFFFF',
  radius: 12, ribbon_text: null, status_colors: {},
};

// A legacy single-value key (ENGINE_KEYS in OverlaysEditor.tsx) kept in old
// configs that predate ELEMENT_ORDER's current names -- no control here
// offers to turn it on or off, so a profile that still has it must keep it
// on save rather than silently losing it.
const LEGACY_RATING_KEY = 'rating';

// Build the preview payload's `elements` -- same filtering as
// OverlaysEditor.tsx's buildPayloadStyle: only enabled elements, and only
// their non-default fields, since the preview endpoint's `style` REPLACES
// `elements` wholesale rather than merging per-element.
function buildElementsPayload(draftStyle: Record<string, DocElementState>): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const key of ELEMENT_ORDER) {
    const e = draftStyle[key];
    if (!e || e.enabled !== true) continue;
    const el: Record<string, unknown> = { enabled: true };
    if (e.position != null && e.position !== '') el.position = e.position;
    if (typeof e.x_offset === 'number') el.x_offset = e.x_offset;
    if (typeof e.y_offset === 'number') el.y_offset = e.y_offset;
    if (e.size != null) el.size = e.size;
    if (e.color) el.color = e.color;
    if (e.back_color) el.back_color = e.back_color;
    if (e.radius != null) el.radius = e.radius;
    if (typeof e.padding_x === 'number') el.padding_x = e.padding_x;
    if (typeof e.padding_y === 'number') el.padding_y = e.padding_y;
    if (typeof e.opacity === 'number' && e.opacity < 1.0) el.opacity = e.opacity;
    if ((key === 'status' || key === 'trending_rank') && e.full_width === false) el.full_width = false;
    out[key] = el;
  }
  return out;
}

// Anchor -> CSS corner for the canvas's draggable chips (deliberately
// APPROXIMATE -- see the canvas section's own comment further down).
const ANCHOR_CSS: Record<string, React.CSSProperties> = {
  top_left: { top: 0, left: 0 },
  top_center: { top: 0, left: '50%', transform: 'translateX(-50%)' },
  top_right: { top: 0, right: 0 },
  center_left: { top: '50%', left: 0, transform: 'translateY(-50%)' },
  center: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' },
  center_right: { top: '50%', right: 0, transform: 'translateY(-50%)' },
  bottom_left: { bottom: 0, left: 0 },
  bottom_center: { bottom: 0, left: '50%', transform: 'translateX(-50%)' },
  bottom_right: { bottom: 0, right: 0 },
};

interface DragState { key: string; mode: 'move' | 'resize'; startX: number; startY: number; startOffX: number; startOffY: number; startSize: number }

export function OverlaysPage(): React.ReactElement {
  const { push } = useToast();
  const [parsed, setParsed] = useState<any>(null);
  const [presets, setPresets] = useState<OverlaysPreset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  // Named, reusable styles (overlays.user_styles) -- not tied to any
  // library, unlike a profile. Legacy's "Global (defaults)" scope edits
  // these (PUT /api/overlays/{name}), never overlays.style itself.
  const [userStyles, setUserStyles] = useState<OverlaysUserStyle[]>([]);
  const [selectedStyle, setSelectedStyle] = useState<string | null>(null);
  const [newStyleOpen, setNewStyleOpen] = useState(false);
  const [newStyleName, setNewStyleName] = useState('');
  const [newStyleFromPreset, setNewStyleFromPreset] = useState('');
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<'profiles' | 'presets'>('profiles');
  // Operator-requested (2026-11-09, revised): Overlay Studio holds base
  // settings, on/off + fine-tuning (ElementControls, position controls
  // hidden -- the canvas below covers those by drag) AND the live canvas,
  // all together -- not split across tabs. Settings is just Format Rules,
  // the one thing genuinely separate from day-to-day badge placement.
  const [editorTab, setEditorTab] = useState<'studio' | 'settings'>('studio');
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newFromPreset, setNewFromPreset] = useState<string>('');
  const [helpResp, setHelpResp] = useState<HelpResp | null>(null);
  const [dateStyleErr, setDateStyleErr] = useState<string | null>(null);

  // One entry per ELEMENT_ORDER key -- the single source of truth for both
  // "is this element on" (enabled) and all its fine-tuning.
  const [draftStyle, setDraftStyle] = useState<Record<string, DocElementState>>({});
  const [draftBase, setDraftBase] = useState<BaseDoc>(DEFAULT_BASE);
  const [preserveLegacyRating, setPreserveLegacyRating] = useState(false);

  const [libraryNames, setLibraryNames] = useState<string[]>([]);
  const [previewLibrary, setPreviewLibrary] = useState('');
  const [itemSearch, setItemSearch] = useState('');
  const [itemResults, setItemResults] = useState<ItemLightView[]>([]);
  const [previewItem, setPreviewItem] = useState<ItemLightView | null>(null);
  const [livePreview, setLivePreview] = useState<OverlayPreviewEntry | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [naturalSize, setNaturalSize] = useState({ w: 1000, h: 1500 });
  const dragRef = useRef<DragState | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => { getLibraries().then((d) => setLibraryNames(d.libraries.map((l) => l.name))).catch(() => {}); }, []);
  useEffect(() => { getHelp().then(setHelpResp).catch(() => {}); }, []);
  useEffect(() => {
    if (libraryNames.length && !previewLibrary) setPreviewLibrary(libraryNames[0]);
  }, [libraryNames, previewLibrary]);

  // Item search, debounced -- mirrors OverlaysEditor's own preview picker.
  useEffect(() => {
    if (!previewLibrary) { setItemResults([]); return; }
    const t = window.setTimeout(() => {
      getItemsLight(previewLibrary, { limit: 20, offset: 0, search: itemSearch || undefined })
        .then((d) => setItemResults(d.items))
        .catch(() => setItemResults([]));
    }, 300);
    return () => window.clearTimeout(t);
  }, [previewLibrary, itemSearch]);

  const load = useCallback(() => {
    setError(null);
    Promise.all([getConfig(), getOverlays()])
      .then(([cfg, ov]) => {
        setParsed(cfg.parsed);
        setPresets(ov.presets);
        setUserStyles((ov.user_styles ?? []).map((u) => ({ name: u.name, style: u.style })));
      })
      .catch((e: any) => setError(e?.message ?? 'Could not load overlay profiles.'));
  }, []);

  useEffect(() => { load(); }, [load]);

  const profiles: Record<string, Profile> = parsed?.overlays?.profiles ?? {};

  const usedBy = useCallback((name: string) => {
    const libs = parsed?.libraries ?? {};
    return Object.entries(libs).filter(([, l]: [string, any]) => l?.overlay_profile === name).map(([n]) => n);
  }, [parsed]);

  // TV-only elements (status/network) only make sense for a profile actually
  // used by a TV library -- null (show everything) when unassigned or mixed.
  const collectionTypeFor = useCallback((name: string): string | null => {
    const libs = usedBy(name);
    if (!libs.length) return null;
    const t = parsed?.libraries?.[libs[0]]?.type;
    return t === 'movies' || t === 'tvshows' ? t : null;
  }, [usedBy, parsed]);

  const selectProfile = (name: string) => {
    setSelected(name);
    setSelectedStyle(null);
    setEditorTab('studio');
    const prof = profiles[name] ?? {};
    const style = (prof.style as any) ?? {};
    const flatElements: string[] = Array.isArray(prof.elements) ? prof.elements.map(String) : [];
    const styleEls = (style.elements as Record<string, any>) ?? {};
    const nextStyle: Record<string, DocElementState> = {};
    for (const key of ELEMENT_ORDER) {
      nextStyle[key] = { ...(styleEls[key] ?? {}), enabled: flatElements.includes(key) };
    }
    setDraftStyle(structuredClone(nextStyle));
    setDraftBase({
      base_position: style.base_position ?? DEFAULT_BASE.base_position,
      margin: style.margin ?? DEFAULT_BASE.margin,
      font_size: style.font_size ?? DEFAULT_BASE.font_size,
      bg: style.bg ?? DEFAULT_BASE.bg,
      fg: style.fg ?? DEFAULT_BASE.fg,
      radius: style.radius ?? DEFAULT_BASE.radius,
      ribbon_text: (typeof style.ribbon_text === 'string') ? style.ribbon_text : null,
      status_colors: structuredClone(style.status_colors ?? {}),
    });
    setPreserveLegacyRating(flatElements.includes(LEGACY_RATING_KEY));
  };

  // A named style has no flat elements list (that concept exists only to
  // fix a profile-specific bug, S9d4c) -- style.elements[key].enabled is
  // already authoritative on its own here.
  const selectUserStyle = (entry: { name: string; style: any }) => {
    setSelected(null);
    setSelectedStyle(entry.name);
    setNewStyleName(entry.name);
    setEditorTab('studio');
    const style = entry.style ?? {};
    const styleEls = (style.elements as Record<string, any>) ?? {};
    const nextStyle: Record<string, DocElementState> = {};
    for (const key of ELEMENT_ORDER) nextStyle[key] = { ...(styleEls[key] ?? {}) };
    setDraftStyle(structuredClone(nextStyle));
    setDraftBase({
      base_position: style.base_position ?? DEFAULT_BASE.base_position,
      margin: style.margin ?? DEFAULT_BASE.margin,
      font_size: style.font_size ?? DEFAULT_BASE.font_size,
      bg: style.bg ?? DEFAULT_BASE.bg,
      fg: style.fg ?? DEFAULT_BASE.fg,
      radius: style.radius ?? DEFAULT_BASE.radius,
      ribbon_text: (typeof style.ribbon_text === 'string') ? style.ribbon_text : null,
      status_colors: structuredClone(style.status_colors ?? {}),
    });
    setPreserveLegacyRating(false);
  };

  const saveNamedStyle = () => {
    if (!newStyleName.trim()) return;
    setSaving(true);
    const name = newStyleName.trim();
    saveOverlay(name, { ...draftBase, elements: draftStyle })
      .then(() => { push({ message: `Saved "${name}"` }); setSelectedStyle(name); load(); })
      .catch((e: any) => push({ message: e?.message ?? 'Could not save.', tone: 'danger' }))
      .finally(() => setSaving(false));
  };

  const createNamedStyle = () => {
    if (!newStyleName.trim()) return;
    const preset = presets.find((p) => p.name === newStyleFromPreset);
    const name = newStyleName.trim();
    saveOverlay(name, preset?.style ?? {})
      .then(() => {
        push({ message: `Created "${name}"` });
        setNewStyleOpen(false);
        load();
        selectUserStyle({ name, style: preset?.style ?? {} });
        setTab('profiles');
        setNewStyleFromPreset('');
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not create style.', tone: 'danger' }));
  };

  const updateElement = (key: string, patch: Partial<DocElementState>): void => {
    setDraftStyle((prev) => ({ ...prev, [key]: { ...(prev[key] || {}), ...patch } }));
  };
  const patchBase = (k: keyof OverlayDoc, v: unknown): void => {
    setDraftBase((prev) => ({ ...prev, [k]: v }) as BaseDoc);
  };
  const updateStatusColor = (statusKey: string, color: string | null): void => {
    setDraftBase((prev) => {
      const next = { ...(prev.status_colors || {}) };
      if (color) next[statusKey] = color; else delete next[statusKey];
      return { ...prev, status_colors: next };
    });
  };

  const setDateStyle = async (v: string): Promise<void> => {
    if (!parsed) return;
    const updated = { ...parsed, status_overlay: { ...(parsed.status_overlay || {}), date_style: v } };
    setDateStyleErr(null);
    try {
      await putConfig(yamlDump(updated, { noRefs: true, lineWidth: -1 }));
      setParsed(updated);
    } catch (e: any) {
      setDateStyleErr(e?.message ?? 'save failed');
    }
  };

  const saveSelected = () => {
    if (!selected || !parsed) return;
    setSaving(true);
    const next = structuredClone(parsed);
    next.overlays ??= {};
    next.overlays.profiles ??= {};
    const existing = next.overlays.profiles[selected] ?? {};
    const flatElements: string[] = ELEMENT_ORDER.filter((k) => draftStyle[k]?.enabled === true);
    if (preserveLegacyRating) flatElements.push(LEGACY_RATING_KEY);
    next.overlays.profiles[selected] = {
      ...existing,
      elements: flatElements,
      style: { ...draftBase, elements: draftStyle },
    };
    putConfig(yamlDump(next, { noRefs: true, lineWidth: -1 }))
      .then(() => { push({ message: `Saved ${selected}` }); setParsed(next); })
      .catch((e: any) => push({ message: e?.message ?? 'Could not save.', tone: 'danger' }))
      .finally(() => setSaving(false));
  };

  // Operator-requested (2026-11-09): deleting is the one profile-management
  // action Studio was missing (creation already existed via "New profile").
  // Confirm first since a profile currently assigned to a library would
  // silently fall back to global defaults on apply -- not catastrophic, but
  // worth a heads-up before an irreversible write.
  const deleteProfile = (name: string) => {
    if (!parsed) return;
    const libs = usedBy(name);
    const msg = libs.length
      ? `Delete profile "${name}"? ${libs.join(', ')} ${libs.length === 1 ? 'uses' : 'use'} it and will fall back to global defaults. This can't be undone.`
      : `Delete profile "${name}"? This can't be undone.`;
    if (!window.confirm(msg)) return;
    const next = structuredClone(parsed);
    if (next.overlays?.profiles) delete next.overlays.profiles[name];
    putConfig(yamlDump(next, { noRefs: true, lineWidth: -1 }))
      .then(() => {
        push({ message: `Deleted ${name}` });
        setParsed(next);
        if (selected === name) setSelected(null);
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not delete.', tone: 'danger' }));
  };

  const deleteNamedStyle = (name: string) => {
    if (!parsed || !window.confirm(`Delete style "${name}"? This can't be undone.`)) return;
    const next = structuredClone(parsed);
    if (next.overlays?.user_styles) delete next.overlays.user_styles[name];
    putConfig(yamlDump(next, { noRefs: true, lineWidth: -1 }))
      .then(() => {
        push({ message: `Deleted ${name}` });
        setParsed(next);
        setUserStyles((prev) => prev.filter((u) => u.name !== name));
        if (selectedStyle === name) setSelectedStyle(null);
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not delete.', tone: 'danger' }));
  };

  // Debounced live preview -- sends the FULL doc (base + elements), same as
  // OverlaysEditor's own buildPayloadStyle(doc) call, since the preview
  // endpoint replaces each top-level style key wholesale rather than
  // merging it field-by-field.
  useEffect(() => {
    if (!(selected || selectedStyle) || !previewItem) { setLivePreview(null); return; }
    const style = { ...draftBase, elements: buildElementsPayload(draftStyle) };
    const t = window.setTimeout(() => {
      setPreviewBusy(true);
      // `profile` must be an overlays.profiles name (the backend 400s on an
      // unknown one) -- a named user_style has no such entry, so omit it;
      // the posted `style` already carries everything that matters.
      previewOverlay(style, [previewItem.item_id], false, selected || null)
        .then((r) => setLivePreview(r.previews?.[0] ?? null))
        .catch(() => {})
        .finally(() => setPreviewBusy(false));
    }, 300);
    return () => window.clearTimeout(t);
  }, [selected, selectedStyle, previewItem, draftBase, draftStyle]);

  const createProfile = () => {
    if (!newName.trim() || !parsed) return;
    const preset = presets.find((p) => p.name === newFromPreset);
    const next = structuredClone(parsed);
    next.overlays ??= {};
    next.overlays.profiles ??= {};
    next.overlays.profiles[newName.trim()] = preset
      ? { elements: elementsFromStyle(preset.style), style: preset.style }
      : { elements: [] };
    putConfig(yamlDump(next, { noRefs: true, lineWidth: -1 }))
      .then(() => {
        push({ message: `Created ${newName.trim()}` });
        setParsed(next);
        setNewOpen(false);
        selectProfile(newName.trim());
        setTab('profiles');
        setNewName('');
        setNewFromPreset('');
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not create profile.', tone: 'danger' }));
  };

  // Drag/resize (SPEC_S14_UI.md P5 second pass). Pointer capture on the chip
  // itself means move/up keep firing on it even once the cursor leaves its
  // bounds -- no window-level listener needed. x/y deltas are measured in
  // screen px and scaled into the image's own pixel space so the offset
  // written to config matches what the server-side renderer actually uses.
  const onChipPointerDown = (key: string, mode: 'move' | 'resize') => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const cur = draftStyle[key] ?? {};
    dragRef.current = {
      key, mode, startX: e.clientX, startY: e.clientY,
      startOffX: cur.x_offset ?? 0, startOffY: cur.y_offset ?? 0, startSize: cur.size ?? 1,
    };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onChipPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const rect = canvasRef.current?.getBoundingClientRect();
    const scale = rect && rect.width > 0 ? naturalSize.w / rect.width : 1;
    const dx = (e.clientX - d.startX) * scale;
    const dy = (e.clientY - d.startY) * scale;
    if (d.mode === 'move') {
      updateElement(d.key, { x_offset: Math.round(d.startOffX + dx), y_offset: Math.round(d.startOffY + dy) });
    } else {
      const next = Math.min(2.5, Math.max(0.4, d.startSize + dy / 150));
      updateElement(d.key, { size: Math.round(next * 100) / 100 });
    }
  };
  const onChipPointerUp = () => { dragRef.current = null; };

  if (error) return <ErrorState title="Could not load overlay profiles" description={error} action={<Button onClick={load}>Retry</Button>} />;
  if (!parsed) return <Skeleton height={200} />;

  const profileNames = Object.keys(profiles).sort();
  const doc: OverlayDoc = { ...draftBase, elements: draftStyle };

  return (
    <div>
      <PageHeader
        title="Overlays"
        description="Profiles decide which badges show on a library's posters."
        actions={<Button variant="primary" onClick={() => setNewOpen(true)}>New profile</Button>}
      />

      <Tabs
        aria-label="Overlays sections"
        items={[{ id: 'profiles', label: `Profiles (${profileNames.length})` }, { id: 'presets', label: 'Preset gallery' }]}
        activeId={tab}
        onChange={(id) => setTab(id as 'profiles' | 'presets')}
      />

      <div style={{ marginTop: 16 }}>
        {tab === 'presets' && (
          <div className="fm-grid-3">
            {presets.map((p) => (
              <Card key={p.name}>
                <CardHeader title={p.name} />
                <CardBody>
                  <p style={{ margin: '0 0 10px', fontSize: 'var(--text-sm)', color: 'var(--text-muted)' }}>{p.description}</p>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
                    {elementsFromStyle(p.style).map((k) => <Badge key={k} tone="accent">{ELEMENT_LABELS[k] ?? k}</Badge>)}
                  </div>
                  <Button size="sm" onClick={() => { setNewFromPreset(p.name); setNewOpen(true); }}>Use as new profile</Button>
                </CardBody>
              </Card>
            ))}
          </div>
        )}

        {tab === 'profiles' && (
          profileNames.length === 0 ? (
            <EmptyState icon="overlays" title="No profiles yet" description="Create one, or start from a preset." action={<Button onClick={() => setTab('presets')}>Browse presets</Button>} />
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: '240px 1fr', gap: 20, alignItems: 'start' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {profileNames.map((name) => (
                  <button
                    key={name}
                    className={['fm-navitem', name === selected ? 'fm-navitem--active' : ''].filter(Boolean).join(' ')}
                    style={{ width: '100%' }}
                    onClick={() => selectProfile(name)}
                  >
                    <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</span>
                    <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>{(profiles[name]?.elements ?? []).length}</span>
                  </button>
                ))}

                {/* Named styles (overlays.user_styles) -- legacy's "Global
                    (defaults)" scope. Not tied to any library; a style here
                    is a reusable starting point, assigned to a profile
                    later (or used directly via a preset). */}
                <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border)', fontSize: 'var(--text-xs)', color: 'var(--text-faint)', fontWeight: 600, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  Named styles
                  <button className="btn-ghost" onClick={() => { setNewStyleName(''); setNewStyleFromPreset(''); setNewStyleOpen(true); }} title="New named style">+</button>
                </div>
                {userStyles.map((u) => (
                  <button
                    key={u.name}
                    className={['fm-navitem', u.name === selectedStyle ? 'fm-navitem--active' : ''].filter(Boolean).join(' ')}
                    style={{ width: '100%' }}
                    onClick={() => selectUserStyle(u)}
                  >
                    <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis' }}>{u.name}</span>
                  </button>
                ))}
              </div>

              {(selected && profiles[selected]) || selectedStyle ? (
                <Card>
                  <CardHeader
                    title={selected ?? newStyleName}
                    action={selected
                      ? (
                        <div style={{ display: 'flex', gap: 8 }}>
                          <Button size="sm" variant="ghost" onClick={() => deleteProfile(selected)}>Delete</Button>
                          <Button size="sm" variant="primary" loading={saving} onClick={saveSelected}>Save</Button>
                        </div>
                      )
                      : (
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <Input value={newStyleName} onChange={(e) => setNewStyleName(e.target.value)} placeholder="style name" style={{ width: 160 }} />
                          <Button size="sm" variant="ghost" onClick={() => deleteNamedStyle(selectedStyle as string)}>Delete</Button>
                          <Button size="sm" variant="primary" loading={saving} onClick={saveNamedStyle}>Save as</Button>
                        </div>
                      )}
                  />
                  <CardBody>
                    <div style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)', marginBottom: 14 }}>
                      {selected
                        ? (usedBy(selected).length > 0 ? `Used by ${usedBy(selected).join(', ')}` : 'Not assigned to any library yet')
                        : 'Named style — not assigned to any library. Saving writes (or renames into) overlays.user_styles.'}
                    </div>

                    <Tabs
                      aria-label="Overlay editor sections"
                      items={[
                        { id: 'studio', label: 'Overlay Studio' },
                        { id: 'settings', label: 'Settings' },
                      ]}
                      activeId={editorTab}
                      onChange={(id) => setEditorTab(id as typeof editorTab)}
                    />

                    <div style={{ marginTop: 16 }}>
                      {editorTab === 'studio' && (
                        <div className="overlay-studio-modern">
                          <BaseSettings doc={doc} patchBase={patchBase} />
                          <div style={{ marginTop: 12 }}>
                            {/* hidePositionControls: the canvas below already
                                covers position/size by drag -- this is on/off
                                + the fine-tuning the canvas can't do (color,
                                padding, opacity, status colors, ribbon text). */}
                            <ElementControls
                              doc={doc}
                              updateElement={updateElement}
                              setRibbonText={(v) => patchBase('ribbon_text', v)}
                              updateStatusColor={updateStatusColor}
                              collectionType={selected ? collectionTypeFor(selected) : null}
                              dateStyle={parsed?.status_overlay?.date_style ?? 'relative'}
                              onDateStyleChange={(v) => void setDateStyle(v)}
                              dateStyleErr={dateStyleErr}
                              hidePositionControls
                            />
                          </div>

                          <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--border)' }}>
                            <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 8 }}>
                              Live canvas — drag a badge to reposition, drag its corner to resize
                            </div>
                            <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
                              <Select
                                value={previewLibrary}
                                onChange={(e) => { setPreviewLibrary(e.target.value); setPreviewItem(null); }}
                                options={libraryNames.map((n) => ({ value: n, label: n }))}
                                style={{ maxWidth: 180 }}
                              />
                              <Input
                                placeholder="Search for an item to preview against…"
                                value={itemSearch}
                                onChange={(e) => setItemSearch(e.target.value)}
                                style={{ flex: 1, minWidth: 200 }}
                              />
                            </div>

                            {!previewItem && itemResults.length > 0 && (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginBottom: 12, maxHeight: 160, overflowY: 'auto' }}>
                                {itemResults.map((it) => (
                                  <button
                                    key={it.item_id}
                                    className="fm-navitem"
                                    style={{ width: '100%', textAlign: 'left' }}
                                    onClick={() => setPreviewItem(it)}
                                  >
                                    {it.item_name}
                                  </button>
                                ))}
                              </div>
                            )}

                            {previewItem ? (
                              <OverlayCanvas
                                item={previewItem}
                                onClear={() => setPreviewItem(null)}
                                draftStyle={draftStyle}
                                livePreview={livePreview}
                                previewBusy={previewBusy}
                                naturalSize={naturalSize}
                                onNaturalSize={setNaturalSize}
                                canvasRef={canvasRef}
                                onChipPointerDown={onChipPointerDown}
                                onChipPointerMove={onChipPointerMove}
                                onChipPointerUp={onChipPointerUp}
                              />
                            ) : (
                              <div className="field-hint muted">Pick an item above to see badges drawn on it.</div>
                            )}
                          </div>
                        </div>
                      )}

                      {editorTab === 'settings' && (
                        <div className="overlay-studio-modern">
                          <FormatRulesSection
                            helpResp={helpResp}
                            selectedIds={previewItem ? [previewItem.item_id] : []}
                            previews={livePreview ? [livePreview] : []}
                          />
                        </div>
                      )}
                    </div>
                  </CardBody>
                </Card>
              ) : (
                <EmptyState icon="overlays" title="Pick a profile or style" description="Select one on the left to edit it." />
              )}
            </div>
          )
        )}
      </div>

      <Dialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        title="New overlay profile"
        actions={<>
          <Button variant="ghost" onClick={() => setNewOpen(false)}>Cancel</Button>
          <Button variant="primary" onClick={createProfile}>Create</Button>
        </>}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Input label="Name" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. anime" />
          <Select
            label="Start from"
            value={newFromPreset}
            onChange={(e) => setNewFromPreset(e.target.value)}
            options={[{ value: '', label: 'Blank (no elements)' }, ...presets.map((p) => ({ value: p.name, label: p.name }))]}
          />
        </div>
      </Dialog>

      <Dialog
        open={newStyleOpen}
        onClose={() => setNewStyleOpen(false)}
        title="New named style"
        actions={<>
          <Button variant="ghost" onClick={() => setNewStyleOpen(false)}>Cancel</Button>
          <Button variant="primary" onClick={createNamedStyle}>Create</Button>
        </>}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Input label="Name" value={newStyleName} onChange={(e) => setNewStyleName(e.target.value)} placeholder="e.g. anime-base" />
          <Select
            label="Start from"
            value={newStyleFromPreset}
            onChange={(e) => setNewStyleFromPreset(e.target.value)}
            options={[{ value: '', label: 'Blank (no elements)' }, ...presets.map((p) => ({ value: p.name, label: p.name }))]}
          />
        </div>
      </Dialog>
    </div>
  );
}

interface OverlayCanvasProps {
  item: ItemLightView;
  onClear: () => void;
  draftStyle: Record<string, DocElementState>;
  livePreview: OverlayPreviewEntry | null;
  previewBusy: boolean;
  naturalSize: { w: number; h: number };
  onNaturalSize: (s: { w: number; h: number }) => void;
  canvasRef: React.RefObject<HTMLDivElement>;
  onChipPointerDown: (key: string, mode: 'move' | 'resize') => (e: React.PointerEvent) => void;
  onChipPointerMove: (e: React.PointerEvent) => void;
  onChipPointerUp: () => void;
}

// The draggable layer (chips, approximate anchor math) sits over the plain
// base poster; the debounced server-rendered image beside it is the actual
// truth. The chip is a deliberately APPROXIMATE stand-in for the real
// renderer's exact text/padding/font math (Pillow on the server, not CSS)
// -- it only needs to be close enough to grab and drag.
function OverlayCanvas({
  item, onClear, draftStyle, livePreview, previewBusy,
  naturalSize, onNaturalSize, canvasRef, onChipPointerDown, onChipPointerMove, onChipPointerUp,
}: OverlayCanvasProps): React.ReactElement {
  const baseImage = livePreview?.base_image;
  const displayWidth = 280;
  const displayHeight = Math.round(displayWidth * (naturalSize.h / naturalSize.w));
  const enabledKeys = ELEMENT_ORDER.filter((k) => draftStyle[k]?.enabled === true);

  return (
    <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', alignItems: 'flex-start' }}>
      <div>
        <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)', marginBottom: 6 }}>
          {item.item_name} <button className="btn btn-ghost" style={{ marginLeft: 6 }} onClick={onClear}>change</button>
        </div>
        <div
          ref={canvasRef}
          style={{
            position: 'relative', width: displayWidth, height: displayHeight,
            background: 'var(--surface-3)', borderRadius: 'var(--r-md)', overflow: 'hidden',
            touchAction: 'none',
          }}
        >
          {baseImage && (
            <img
              src={baseImage}
              alt=""
              style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block', pointerEvents: 'none' }}
              onLoad={(e) => {
                const img = e.currentTarget;
                if (img.naturalWidth && img.naturalHeight) onNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
              }}
            />
          )}
          {enabledKeys.map((key) => {
            const e = draftStyle[key] ?? {};
            const scale = displayWidth / naturalSize.w;
            const anchor = ANCHOR_CSS[e.position || 'top_left'] ?? ANCHOR_CSS.top_left;
            const nudge: React.CSSProperties = {
              marginLeft: (e.x_offset ?? 0) * scale,
              marginTop: (e.y_offset ?? 0) * scale,
            };
            const size = e.size ?? 1;
            return (
              <div
                key={key}
                onPointerDown={onChipPointerDown(key, 'move')}
                onPointerMove={onChipPointerMove}
                onPointerUp={onChipPointerUp}
                style={{
                  position: 'absolute', ...anchor, ...nudge,
                  padding: `${2 * size}px ${5 * size}px`, fontSize: 10 * size,
                  background: 'rgba(0,0,0,0.72)', color: '#fff', borderRadius: 4,
                  cursor: 'move', userSelect: 'none', whiteSpace: 'nowrap',
                  border: '1px solid rgba(255,255,255,0.3)',
                }}
                title={`${ELEMENT_LABELS[key] ?? key} — drag to move`}
              >
                {ELEMENT_LABELS[key] ?? key}
                <span
                  onPointerDown={(ev) => { ev.stopPropagation(); onChipPointerDown(key, 'resize')(ev); }}
                  style={{
                    position: 'absolute', right: -4, bottom: -4, width: 8, height: 8,
                    background: 'var(--accent)', borderRadius: '50%', cursor: 'nwse-resize',
                  }}
                  title="Drag to resize"
                />
              </div>
            );
          })}
        </div>
      </div>

      <div>
        <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)', marginBottom: 6 }}>
          Server-rendered preview {previewBusy && '(updating…)'}
        </div>
        <div style={{
          width: displayWidth, height: displayHeight, background: 'var(--surface-3)',
          borderRadius: 'var(--r-md)', overflow: 'hidden', display: 'flex',
          alignItems: 'center', justifyContent: 'center',
        }}>
          {livePreview?.error ? (
            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--danger)', padding: 8 }}>{livePreview.error}</span>
          ) : livePreview?.image ? (
            <img src={livePreview.image} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : (
            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>Rendering…</span>
          )}
        </div>
      </div>
    </div>
  );
}

export default OverlaysPage;

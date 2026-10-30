// Overlay editor tab (SPEC S9 \u00a7D). Presets list on the left, per-element controls
// plus a live server-rendered preview grid on the right. Selecting a preset or an
// existing user-style deep-copies it into local edit state first ("clone-before-edit");
// built-in presets themselves are never mutated here — only read as starting points.
import { useEffect, useRef, useState } from 'react';
import { getConfig, putConfig, getLibraries, getItemsLight, getOverlays,
       installLogoPack, logoPackStatus, previewOverlay, saveOverlay, getHelp,
       getDefaultFormatRules, getJellyfinLibraries,
       getPacks, deletePack, getKometaSources, importKometaRemote } from '../api';
import type { JfLibrary, ImagePackInfo } from '../api';
import type { HelpResp } from '../types';
import { load as yamlLoad, dump as yamlDump } from 'js-yaml';
import type { ItemLightView, LibItemsLightResp } from '../types';

const PREVIEW_PAGE_SIZE = 200;   // same page size as LibraryBrowser.tsx's own list

const ELEMENT_ORDER = [
  'imdb', 'rt_critic', 'rt_audience', 'metacritic', 'tmdb', 'letterboxd', 'trakt',
  'resolution', 'hdr', 'audio', 'network', 'status', 'trending_rank', 'ribbon',
] as const;

const ELEMENT_LABELS: Record<string, string> = {
  imdb: 'IMDb', rt_critic: 'RT critic', rt_audience: 'RT audience',
  metacritic: 'Metacritic', tmdb: 'TMDb star rating', letterboxd: 'Letterboxd', trakt: 'Trakt',
  resolution: 'Resolution', hdr: 'HDR / Dolby Vision', audio: 'Audio codec / Atmos',
  // 2026-10-23: streaming/broadcast network from TMDb's own `networks` field
  // (e.g. "Netflix") — TV libraries only. Badge images fetched lazily from
  // Kometa-Team/Default-Images (engine.network_logos), not a flat logo pack
  // (280+ possible names, too many to pre-declare).
  network: 'Network / streaming service',
  // S9g1/K7a: TV show status from Sonarr (new season soon, returning, ended, ...) — TV libraries only.
  status: 'TV status (Sonarr)',
  // S9g2/K7b: "#<rank>" badge for items in a Trending tab list's top 10 (rank_overlay on).
  trending_rank: 'Trending rank (MDBList)',
  ribbon: 'Ribbon text',
};

// "status"/"network" only ever do anything for TV libraries (TV status from
// Sonarr; TMDb's `networks` field doesn't exist for movies).
const TV_ONLY_KEYS = new Set(['status', 'network']);
// "rating" is a legacy generic TMDb-star key with no checkbox of its own in
// this tab (kept for back-compat with configs written before the richer
// per-provider rating badges below existed) -- not part of ELEMENT_ORDER.
const _LEGACY_ENGINE_KEYS = ['rating'] as const;
// Fixed 2026-10-25 (operator-reported "IMDb ribbon/RT critic/audience/
// Metacritic/TMDb/Letterboxd/Trakt/ribbon text never actually show up on a
// real poster, only in this tab's own preview"): the apply engine now
// understands every ELEMENT_ORDER key, not just the plain text/image ones --
// plan_builder._overlay_poster passes the K10 rating chain's values through
// as `rating_<key>` (same convention this tab's preview already used, see
// routes_settings.py's `_format_rating_value`) and `_apply_style_doc` gates
// enabled-ness on whatever this editor saves below. Previously only
// resolution/hdr/audio/network/rating/status/trending_rank made it into the
// flat list a real apply reads -- the rest only ever reached this tab's
// preview renderer. See docs/HISTORY.md "Badge-visibility gap" (now closed).
const ENGINE_KEYS: readonly string[] = [...new Set([..._LEGACY_ENGINE_KEYS, ...ELEMENT_ORDER])];

// Fixed 2026-10-19 (operator-reported "next airing/coming soon doesn't
// work"): plan_builder._profile_overlay_names() gates every applied badge on
// a FLAT `profile.elements` list (or the library's own `overlays` list when
// a profile has none) -- never on this editor's richer per-element
// `style.elements.<key>.enabled` flags. Saving a profile here used to write
// ONLY `{style: doc}`, silently discarding whatever flat `elements` list the
// engine actually reads -- so every checkbox in this tab was cosmetic for
// any profile saved through it (confirmed live: the operator's "TV status
// (Sonarr)" checkbox was ON in style.elements.status but never reached the
// real apply, because Shows-auto's flat `elements` key no longer existed
// after being saved here once). effectiveElementNames/seedEnabledFromStyle
// below make the flat list derived FROM these checkboxes on save, and seed
// the checkboxes FROM the current flat list on load, so this editor is the
// single source of truth for every key the engine understands.
function effectiveElementNames(cfg: any, scope: string, prof: any): string[] {
  const fromProfile = prof?.elements;
  if (Array.isArray(fromProfile)) return fromProfile.map(String);
  const fromLibrary = cfg?.libraries?.[scope]?.overlays;
  return Array.isArray(fromLibrary) ? fromLibrary.map(String) : [];
}

function seedEnabledFromEffective(doc: OverlayDoc, effectiveNames: string[]): OverlayDoc {
  const next: OverlayDoc = { ...doc, elements: { ...doc.elements } };
  for (const key of ENGINE_KEYS) {
    const e = next.elements[key] ?? {};
    if (e.enabled === undefined) {
      next.elements[key] = { ...e, enabled: effectiveNames.includes(key) };
    }
  }
  return next;
}

function flatElementsFromDoc(doc: OverlayDoc): string[] {
  return ENGINE_KEYS.filter((key) => doc.elements[key]?.enabled === true);
}

const GRID_ROWS: (string[])[] = [
  ['top_left','top_center','top_right'],
  ['center_left','center','center_right'],
  ['bottom_left','bottom_center','bottom_right'],
];
const ALL_POSITIONS = GRID_ROWS.flat();

interface DocElementState {
  enabled?: boolean;
  position?: string | null;   // ''/null => follow base anchor
  x_offset?: number; y_offset?: number; size?: number | null;
  color?: string; back_color?: string; radius?: number | null;
  padding_x?: number; padding_y?: number; opacity?: number;
  group?: string; weight?: number;
  full_width?: boolean;   // "status"/"trending_rank" only — see ElementControls
}
export interface OverlayDoc {
  base_position: string; margin: number; font_size: number; bg: string; fg: string;
  radius: number; ribbon_text?: string | null; elements: Record<string, DocElementState>;
  status_colors?: Record<string, string>;
}

// TV status (Sonarr) ribbon colours (engine.tv_status.STATUS_COLORS), fine-tunable
// per status value via overlays.style.status_colors -- shown 6-digit (no alpha) for
// a plain colour swatch; backend accepts either #RRGGBB or #RRGGBBAA.
const STATUS_COLOR_KEYS: {key:string; label:string; builtin:string}[] = [
  {key:'new_series', label:'New series', builtin:'#16A34A'},
  {key:'new_season_soon', label:'New season soon', builtin:'#0D9488'},
  {key:'new_season_started', label:'New season started', builtin:'#2563EB'},
  {key:'upcoming_episode', label:'Upcoming episode', builtin:'#4F46E5'},
  {key:'upcoming_finale', label:'Upcoming finale', builtin:'#D97706'},
  {key:'season_finale_aired', label:'Season finale aired', builtin:'#EA580C'},
  {key:'final_episode_aired', label:'Final episode aired', builtin:'#DC2626'},
  {key:'returning', label:'Returning', builtin:'#0891B2'},
  {key:'ended', label:'Ended', builtin:'#475569'},
  {key:'canceled', label:'Canceled', builtin:'#991B1B'},
];

function deepClone<T>(v: T): T { return JSON.parse(JSON.stringify(v)); }

function normalize(docIn: any): OverlayDoc {
  const d: OverlayDoc = {
    base_position: docIn?.base_position ?? 'top_left',
    margin: docIn?.margin ?? 20,
    font_size: docIn?.font_size ?? 42,
    bg: docIn?.bg ?? '#000000B0',
    fg: docIn?.fg ?? '#FFFFFF',
    radius: docIn?.radius ?? 12,
    ribbon_text: (docIn && typeof docIn.ribbon_text === 'string') ? docIn.ribbon_text : null,
    elements: {},
    status_colors: deepClone((docIn && docIn.status_colors) || {}),
  };
  for (const key of ELEMENT_ORDER) {
    d.elements[key] = deepClone((docIn?.elements && docIn.elements[key]) || {});
  }
  return d;
}

interface PresetEntry { name: string; description?: string; style: any }

interface Props { initialLibrary?: string | null }

export default function OverlaysEditor({ initialLibrary }: Props) {
  // ---- data -----------------------------------------------------------------
  const [presets, setPresets] = useState<PresetEntry[]>([]);
  const [userStyles, setUserStyles] = useState<{name:string;style:any}[]>([]);
  const [activeName, setActiveName] = useState<string>('');   // '' => unsaved clone of a preset
  const [sourcePreset, setSourcePreset] = useState<string>('');

  // S9d2-fix step 3: edit scope — 'global' (overlays.user_styles) or a Jellyfin
  // library name (edits overlays.profiles.<lib-slug> assigned to that library).
  const [scope, setScope] = useState<string>(initialLibrary ?? 'global');   // 'global' | library name


  const [jfLibs, setJfLibs] = useState<JfLibrary[]>([]);
  const [doc, setDoc] = useState<OverlayDoc | null>(null);
  const [saveMsg, setSaveMsg] = useState<string|null>(null);
  const [saveErr, setSaveErr] = useState<string|null>(null);
  const [savingAs, setSavingAs] = useState('');
  // Operator feedback 2026-10-19: Format rules (K9 — which exact codec/resolution
  // tiers trigger a badge) felt like redundant clutter sitting next to the plain
  // on/off element checkboxes. Split into its own sub-tab so the common case
  // (turn an element on/off) isn't crowded by this fine-tuning layer.
  const [editTab, setEditTab] = useState<'elements' | 'advanced'>('elements');

  // Logo packs (S9e4 task 3): ordered list of sources from global config, loaded on mount.
  // Only "kometa" is offered in the UI (the operator-run "custom" flat-logos/
  // option was removed 2026-10-22) -- any stray "custom" left in an old
  // config is simply dropped from display state here, not an error.
  const [logoPacks, setLogoPacks] = useState<'kometa'[]>([]);
  // Global config snapshot, kept around just so the "status" element's date
  // style (status_overlay.date_style — not part of the per-profile style
  // doc, same setting the Settings page edits) can be changed right here
  // where the operator is already looking at the TV status element, instead
  // of only in Settings. Round-tripped through the same PUT /api/config the
  // Settings page uses — patch one field, re-dump the whole parsed doc.
  const [globalConfig, setGlobalConfig] = useState<any>(null);
  const [dateStyleErr, setDateStyleErr] = useState<string | null>(null);
  useEffect(() => {
    getConfig()
      .then((d) => {
        setGlobalConfig(d.parsed);
        const v = (d.parsed?.overlays?.logo_packs as string[])
          ?? (typeof d.parsed?.overlays?.logo_pack === 'string' ? [d.parsed.overlays.logo_pack] : []);
        setLogoPacks((v ?? []).filter((x): x is 'kometa' => x === 'kometa'));
      })
      .catch(()=>{ /* keep default */ });
  }, []);

  async function setDateStyle(v: string): Promise<void> {
    if (!globalConfig) return;
    const updated = {...globalConfig, status_overlay: {...(globalConfig.status_overlay||{}), date_style: v}};
    setDateStyleErr(null);
    try {
      await putConfig(yamlDump(updated, {noRefs:true}));
      setGlobalConfig(updated);
    } catch (e: any) {
      setDateStyleErr(e?.message ?? 'save failed');
    }
  }

  // S9d4c/2026-10-20: per-profile image packs (category-based matching, e.g.
  // an imported Kometa setup) + "Import Kometa overlay" live together here,
  // scoped to whichever profile is being edited above. Operator feedback
  // (2026-10-20): the old separate "Packs" fieldset (per-pack, per-category
  // checkboxes) was dropped in favour of one Activate/Deactivate button per
  // installed pack, right inside "Installed packs" -- activating a pack now
  // turns ALL its categories on (no more granular per-category override).
  // 2026-10-22: the "Download a known overlay pack" (import-remote/jmxd)
  // fetch form and the "Custom logos" flat-files option were both removed
  // (operator decision) -- the Kometa logo pack + local "Import Kometa
  // overlay" cover the full badge vocabulary on their own.
  const [availPacks, setAvailPacks] = useState<ImagePackInfo[]>([]);
  const [selPacks, setSelPacks] = useState<string[]>([]);
  const [packsMsg, setPacksMsg] = useState<string | null>(null);
  const [packsErr, setPacksErr] = useState<string | null>(null);

  async function refreshPacks(): Promise<void> {
    try { setAvailPacks((await getPacks()).packs ?? []); } catch { /* keep last good */ }
  }
  useEffect(() => { void refreshPacks(); }, []);

  // Download+import a known third-party overlay source (Finmeta fetches the
  // files itself instead of requiring them already on local disk). One known
  // source -> one fixed pack name (source.default_pack_name, from the
  // backend) -- no pack-name typing, no separate "download" panel: the
  // Installed Packs list below IS the single place this lives, same row as
  // Activate/Deactivate once it's in, operator feedback 2026-10-27 ("don't
  // show the same pack in two different places").
  const [sources, setSources] = useState<Record<string, {label:string; zip_url:string; default_pack_name:string}>>({});
  const [rBusySource, setRBusySource] = useState<string | null>(null);
  const [rStatus, setRStatus] = useState<string | null>(null);
  const [rErr, setRErr] = useState<string | null>(null);
  useEffect(() => { getKometaSources().then((d)=>setSources(d.sources)).catch(()=>{}); }, []);

  async function downloadSource(sourceKey: string, overwrite: boolean) {
    setRBusySource(sourceKey); setRErr(null); setRStatus(overwrite ? 'Updating…' : 'Downloading…');
    try {
      const r = await importKometaRemote({
        source: sourceKey, pack_name: sources[sourceKey].default_pack_name,
        dry_run: false, overwrite,
      });
      setRStatus(`Done: ${r.written?.length ?? 0} images.`);
      await refreshPacks();
    } catch (e: any) {
      setRErr(e?.message ?? 'download failed');
      setRStatus(null);
    }
    finally { setRBusySource(null); }
  }


  // 2026-10-20: one Activate/Deactivate button per installed pack, persisted
  // immediately (no separate "Save" step) -- replaces the old PacksSection's
  // per-category checkboxes + explicit save button. Activating turns on
  // every category the pack defines; pack_overrides (the old per-category
  // disable mechanism) is dropped since there's no UI for it any more.
  async function toggleRichPack(pn: string, on: boolean): Promise<void> {
    setPacksErr(null); setPacksMsg(null);
    const profileName = activeName || savingAs;
    try {
      const d = await getConfig();
      let cfg: any; try { cfg = (yamlLoad(d.yaml) as any) ?? {}; } catch { cfg = {}; }
      if (!cfg.overlays || typeof cfg.overlays !== 'object') cfg.overlays = {};
      if (!cfg.overlays.profiles || typeof cfg.overlays.profiles !== 'object') cfg.overlays.profiles = {};
      const prof = cfg.overlays.profiles[profileName];
      if (typeof prof !== 'object' || prof === null) {
        throw new Error(`save the style above first (“Save as “${profileName || 'name'}”) to create this profile`);
      }
      const cur: string[] = Array.isArray(prof.packs) ? prof.packs : [];
      const next = on ? [...cur.filter((x: string) => x !== pn), pn] : cur.filter((x: string) => x !== pn);
      prof.packs = next;
      delete prof.pack_overrides;
      await putConfig(yamlDump(cfg, { noRefs: true, lineWidth: -1 }));
      setSelPacks(next);
      setPacksMsg(`“${pn}” ${on ? 'activated' : 'deactivated'} for “${profileName}”`);
      setTimeout(() => setPacksMsg(null), 3000);
    } catch (e: any) {
      setPacksErr(e?.message ?? 'save failed');
    }
  }

  // 2026-10-20: uninstall a downloaded/imported pack entirely (files +
  // every profile reference to it) -- irreversible, so confirm first.
  async function deleteRichPack(pn: string): Promise<void> {
    if (!window.confirm(`Delete pack "${pn}"? This removes its files and deactivates it everywhere — this can't be undone.`)) return;
    setPacksErr(null); setPacksMsg(null);
    try {
      await deletePack(pn);
      await refreshPacks();
      setSelPacks(selPacks.filter((x) => x !== pn));
      setPacksMsg(`“${pn}” deleted`);
      setTimeout(() => setPacksMsg(null), 3000);
    } catch (e: any) {
      setPacksErr(e?.message ?? 'delete failed');
    }
  }

  // S9d2-fix step 3: when a library is selected, load its overlay_profile into
  // the editor; 'global' keeps today's behavior (user_styles).
  // Populate the installed-packs section's selection from the profile's
  // stored `packs` list (mirrors ProfilesPanel's old selectProfile()).
  function loadPacksFromProfile(prof: any) {
    setSelPacks(Array.isArray(prof?.packs) ? [...prof.packs] : []);
  }

  useEffect(() => {
    setSaveMsg(null); setSaveErr(null);
    if (scope === 'global') {
      getOverlays().then((ov) => {
        setUserStyles((ov.user_styles ?? []).map(u=>({name:u.name, style:u.style})));
      }).catch(()=>{});
      setSelPacks([]);   // packs attach to a library profile, not a global style
      return;
    }
    const lib = jfLibs.find((l)=>l.name===scope);
    if (!lib) return;
    getConfig().then((d) => {
      const cfg = d.parsed ?? {};
      const profName = (cfg.libraries?.[scope]?.overlay_profile as string | undefined) ?? slugify(scope);
      const prof = (cfg.overlays?.profiles?.[profName] as any) ?? null;
      setUserStyles([]);   // hide global styles while editing a library profile
      const effective = effectiveElementNames(cfg, scope, prof);
      if (prof?.style) {
        setActiveName(profName);
        setSourcePreset('');
        setSavingAs(profName);
        setDoc(seedEnabledFromEffective(normalize(deepClone(prof.style)), effective));
        loadPacksFromProfile(prof);
      } else {
        // no profile yet: start from a copy of the global defaults for creation,
        // seeded with whichever elements the library's own (pre-profile) `overlays`
        // list already has on, so creating a profile here doesn't silently turn
        // off whatever is already being applied today.
        getOverlays().then((ov) => {
          const base = ov.presets?.[0]?.style ?? null;
          setActiveName('');
          setSourcePreset(base ? ov.presets![0].name : '');
          setSavingAs(profName);
          setDoc(seedEnabledFromEffective(normalize(base), effective));
        }).catch(()=>{});
        setSelPacks([]);
      }
    }).catch(()=>{});
  }, [scope, jfLibs]);   // eslint-disable-line react-hooks/exhaustive-deps

  // "Sample data" toggle (K10): placeholders ONLY when explicitly on.
  const [sampleData, setSampleData] = useState<boolean>(false);

  // Help text from /api/help (never hardcoded in the frontend).
  const [helpResp, setHelpResp] = useState<HelpResp | null>(null);
  useEffect(() => { getHelp().then(setHelpResp).catch(() => {}); }, []);

  // preview state
  const [libraryNames, setLibraryNames] = useState<string[]>([]);
  // S9h-fix (2026-10-25, operator-reported): the preview item picker used to
  // silently fetch only the first TWO libraries and show only the first 12
  // items with no way to search or pick a different library -- fine for the
  // fixture, useless on a real multi-thousand-item library. Now one library
  // at a time, chosen explicitly, with a text search -- same pattern as
  // LibraryBrowser.tsx's own search input.
  const [previewLibrary, setPreviewLibrary] = useState<string>('');
  const [previewSearch, setPreviewSearch] = useState('');
  const [previewItemsResp, setPreviewItemsResp] = useState<LibItemsLightResp | null>(null);
  const [previewItemsLoadingMore, setPreviewItemsLoadingMore] = useState(false);
  const previewItems = previewItemsResp?.items ?? [];
  const previewReqIdRef = useRef(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [previews, setPreviews] = useState<{item_id:string;image?:string;error?:string;samples?:string[]}[]>([]);
  const [previewBusy, setPreviewBusy] = useState(false);
  const debounceRef = useRef<number|undefined>(undefined);


  useEffect(() => {
    getOverlays().then((ov) => {
      setPresets(ov.presets ?? []);
      setUserStyles((ov.user_styles ?? []).map(u=>({name:u.name, style:u.style})));
    }).catch(()=>{ /* leave empty */ });
    getLibraries().then(d => setLibraryNames(d.libraries.map(l=>l.name))).catch(()=>{});
    getJellyfinLibraries().then(d => setJfLibs(d.libraries)).catch(()=>{});
  }, []);

  // Follow whichever library is being edited (scope): switching the target
  // library tab (e.g. Movies -> Anime) always re-points the preview at that
  // same library -- operator-reported (2026-10-30), previewing Movies items
  // while editing Anime's style is never useful. Only the initial fallback
  // (scope === 'global', nothing picked yet) leaves room for the preview
  // library dropdown's own manual choice to stick between renders.
  useEffect(() => {
    if (!libraryNames.length) return;
    if (scope !== 'global' && libraryNames.includes(scope)) {
      setPreviewLibrary(scope);
    } else if (!previewLibrary) {
      setPreviewLibrary(libraryNames[0]);
    }
  }, [libraryNames, scope]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Selected preview items belong to whichever library was previewed before
  // -- carrying them over to a newly-followed library would silently render
  // stale items from the old one (same bug as above), since Jellyfin item
  // ids aren't scoped to a single library lookup.
  useEffect(() => { setSelectedIds([]); }, [previewLibrary]);

  // Operator-reported (2026-10-30): typing a search outside whatever the
  // first page happened to contain found nothing -- this only ever fetched
  // one unfiltered page and filtered it client-side (see PreviewGrid below,
  // before this fix). Now mirrors App.tsx's own loadLibraryItems/handleSearch:
  // search is server-side (Jellyfin SearchTerm via /items/light?search=),
  // debounced, replacing the current page; clearing it reloads the
  // unfiltered first page the same way. A stale response (library switched
  // or search edited again mid-request) is dropped via previewReqIdRef.
  const previewSearchDebounceRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!previewLibrary) { setPreviewItemsResp(null); return; }
    if (previewSearchDebounceRef.current !== undefined) window.clearTimeout(previewSearchDebounceRef.current);
    const reqId = ++previewReqIdRef.current;
    const fire = () => {
      // Fixed 2026-10-25 (operator-reported "Overlays preview doesn't load" --
      // actually loaded, but after ~2.4 MINUTES): this only needs item
      // id/name/type for the preview item picker, but getItems() hits
      // GET /items (no limit/offset here) -- routes_core.library_items,
      // which builds a full WritePlan for literally every item in the library
      // (TMDb/fanart/etc. lookups, ~7s/item per that route's own comment).
      // getItemsLight() hits /items/light instead -- Jellyfin-only, no plan
      // build -- which is exactly what library_items_light was added for.
      getItemsLight(previewLibrary, {limit: PREVIEW_PAGE_SIZE, offset: 0, search: previewSearch || undefined})
        .then(d => { if (previewReqIdRef.current === reqId) setPreviewItemsResp(d); })
        .catch(() => { if (previewReqIdRef.current === reqId) setPreviewItemsResp(null); });
    };
    previewSearchDebounceRef.current = window.setTimeout(fire, 300);
    return () => { if (previewSearchDebounceRef.current !== undefined) window.clearTimeout(previewSearchDebounceRef.current); };
  }, [previewLibrary, previewSearch]);

  function loadMorePreviewItems(): void {
    if (!previewLibrary || !previewItemsResp?.has_more || previewItemsLoadingMore) return;
    const reqId = ++previewReqIdRef.current;
    setPreviewItemsLoadingMore(true);
    getItemsLight(previewLibrary, {
      limit: PREVIEW_PAGE_SIZE, offset: previewItemsResp.items.length, search: previewSearch || undefined,
    })
      .then(d => {
        if (previewReqIdRef.current !== reqId) return;
        setPreviewItemsResp(prev => prev ? {...d, items: [...prev.items, ...d.items]} : d);
      })
      .catch(() => {})
      .finally(() => { if (previewReqIdRef.current === reqId) setPreviewItemsLoadingMore(false); });
  }

  // Debounced preview refresh whenever doc or selected items change.
  const idsKey = selectedIds.join(',');
  useEffect(() => {
    if (debounceRef.current !== undefined) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(runPreviewOnce, 300);   // SPEC S9 §D: 300 ms debounce
    return () => { if (debounceRef.current!==undefined) window.clearTimeout(debounceRef.current); };
  }, [doc, idsKey, sampleData, scope]); // eslint-disable-line react-hooks/exhaustive-deps

  function runPreviewOnce() { void doPreview(); }

  // Build the style document sent to /api/overlays/preview. Elements whose
  // checkbox is ticked each appear under style.elements with their own position,
  // offsets, size, colors and backdrop (SPEC S9 D step 5), so toggling e.g. IMDb
  // adds elements.imdb to the very next request body. Row controls bind straight
  // into doc.elements[key] via updateElement - there is no global copy here.
  function buildPayloadStyle(d: OverlayDoc): Record<string, unknown> {
    const payloadElements: Record<string, Record<string, unknown>> = {};
    for (const key of ELEMENT_ORDER) {
      const e = d.elements[key];
      if (!e || e.enabled !== true) continue;   // unticked => absent from payload
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
      payloadElements[key] = el;
    }
    return {...d, elements: payloadElements};
  }

  async function doPreview() {
    if (!doc || !selectedIds.length) { setPreviews([]); return; }
    setPreviewBusy(true);
    try {
      const libSel = jfLibs.find((l)=>l.name===scope);
      const profName = scope !== 'global' && libSel?.overlay_profile ? libSel.overlay_profile : null;
      const res = await previewOverlay(buildPayloadStyle(doc), selectedIds.slice(0,4), sampleData, profName);
      setPreviews(res.previews ?? []);
    } catch { /* keep last good previews on transient failure */ } finally {
      setPreviewBusy(false);
    }
  }

  function selectPreset(name: string): void {
    const preset = presets.find((p)=>p.name===name);
    if (!preset?.style) return;
    setActiveName('');            // unsaved until the user names + saves it
    setSourcePreset(name);
    setSavingAs(`my-${slugify(name)}`);
    setDoc(normalize(preset.style));   // normalize deep-copies each element value
  }

  function selectUserStyle(entry: {name:string; style:any}): void {
    setActiveName(entry.name);
    setSourcePreset('');
    setSavingAs(entry.name);
    setDoc(normalize(deepClone(entry.style)));
  }

  async function saveProfileForLibrary(libName: string, profileName: string) {
    if (!doc) return;
    const d = await getConfig();
    let cfg: any; try { cfg = (yamlLoad(d.yaml) as any) ?? {}; } catch { cfg = {}; }
    if (!cfg.overlays || typeof cfg.overlays !== 'object') cfg.overlays = {};
    if (!cfg.overlays.profiles || typeof cfg.overlays.profiles !== 'object') cfg.overlays.profiles = {};
    if (!cfg.libraries || typeof cfg.libraries !== 'object') cfg.libraries = {};
    if (!cfg.libraries[libName] || typeof cfg.libraries[libName] !== 'object') {
      // Config's library entry is a `type`-discriminated union (movies vs.
      // tvshows); a library this editor's scope dropdown offers always comes
      // from Jellyfin's own library list, so it always has a collection_type
      // to seed `type` with. Without this, saving a profile for a library
      // that has never been touched in config.yml before (never opened in
      // the Libraries tab, never had "Managed by Finmeta" toggled) sent just
      // `{overlay_profile: name}` with no `type` -- a 400 "unable to extract
      // tag using discriminator 'type'" from Pydantic, reported live.
      const jfType = jfLibs.find((l) => l.name === libName)?.collection_type;
      if (jfType !== 'movies' && jfType !== 'tvshows') {
        throw new Error(
          `can't create a config entry for "${libName}": unknown or unsupported `
          + `Jellyfin collection type ${jfType ? `"${jfType}"` : '(library not found)'}`);
      }
      cfg.libraries[libName] = { type: jfType };
    }
    // Merge onto whatever the profile already has (packs/pack_overrides/format_rules
    // saved via the other panels in this tab) instead of replacing the whole object —
    // and persist the flat `elements` list the real engine reads, derived from these
    // checkboxes, so it never again falls out of sync with what this editor shows.
    const existingProf = (cfg.overlays.profiles[profileName] && typeof cfg.overlays.profiles[profileName] === 'object')
      ? cfg.overlays.profiles[profileName] : {};
    cfg.overlays.profiles[profileName] = {
      ...existingProf,
      elements: flatElementsFromDoc(doc),
      style: doc as unknown as Record<string,unknown>,
    };
    cfg.libraries[libName].overlay_profile = profileName;
    await putConfig(yamlDump(cfg, { noRefs: true, lineWidth: -1 }));
  }

  async function save() {
    if (!doc || !savingAs.trim()) return;
    setSaveErr(null); setSaveMsg(null);
    try {
      if (scope !== 'global') {
        await saveProfileForLibrary(scope, savingAs.trim());
        setSaveMsg(`Saved profile “${savingAs}” for ${scope}`);
        setTimeout(()=>setSaveMsg(null),3500);
        const d = await getJellyfinLibraries();
        setJfLibs(d.libraries.map((l) => ({ ...l, overlay_profile: l.overlay_profile ?? null })));
        return;
      }
      await saveOverlay(savingAs.trim(), doc as unknown as Record<string,unknown>);
      setSaveMsg(`Saved “${savingAs}”`);
      setTimeout(()=>setSaveMsg(null),3500);
      const ov = await getOverlays();
      setUserStyles((ov.user_styles ?? []).map(u=>({name:u.name, style:u.style})));
      setActiveName(savingAs.trim());
    } catch (e: any) {
      setSaveErr(e?.message ?? 'save failed');
    }
  }

  function updateElement(key: string, patch: Partial<DocElementState>): void {
    setDoc(prev => prev ? ({ ...prev, elements: { ...prev.elements, [key]: { ...(prev.elements[key]||{}), ...patch } } }) : prev);
  }

  function patchBase(k: keyof OverlayDoc, v: unknown): void {
    setDoc(prev => prev ? ({...prev,[k]:v} as OverlayDoc) : null);
  }

  function updateStatusColor(statusKey: string, color: string | null): void {
    setDoc(prev => {
      if (!prev) return prev;
      const next = { ...(prev.status_colors || {}) };
      if (color) next[statusKey] = color; else delete next[statusKey];
      return { ...prev, status_colors: next };
    });
  }

  async function changeLogoPacks(v: 'kometa'[]): Promise<void> {
    try {
      const d = await getConfig();
      let docYaml: any;
      try { docYaml = (yamlLoad(d.yaml) as any) ?? {}; } catch { docYaml = {}; }
      if (!docYaml.overlays || typeof docYaml.overlays !== 'object') docYaml.overlays = {};
      delete docYaml.overlays.logo_pack;   // drop legacy single-value key
      docYaml.overlays.logo_packs = v;
      await putConfig(yamlDump(docYaml, { noRefs: true, lineWidth: -1 }));
      setLogoPacks(v);
    } catch (e:any) { /* leave selection unchanged on failure */ }
  }

  // S9d2-fix step 3: scope selector (Global + all Jellyfin libraries).
  const jfLibsSorted = [...jfLibs].sort((a,b)=>a.name.localeCompare(b.name));
  const scopeSelector = (
    <div className="overlay-save-row" style={{ marginBottom: '8px' }}>
      <label className="form-label" style={{ margin: 0 }}>Edit target</label>
      <select className="config-input" value={scope} onChange={(e)=>setScope(e.target.value)}>
        <option value="global">Global (defaults)</option>
        {jfLibsSorted.map((l) => (
          <option key={l.name} value={l.name}>
            {l.name}{l.overlay_profile ? ` — profile “${l.overlay_profile}”` : ' — no profile'}
          </option>
        ))}
      </select>
      {scope !== 'global' && !jfLibs.find((l)=>l.name===scope)?.overlay_profile && (
        <>
          <button className="btn" onClick={()=>{ setSavingAs(slugify(scope)); void save(); }}>
            Create profile for {scope}
          </button>
          <span className="field-hint muted">Copies the current global values and assigns it to this library.</span>
        </>
      )}
    </div>
  );

  if (!presets.length && !userStyles.length) {
    return <div className="settings-section"><h3>Overlays</h3><div className="field-hint muted">Loading presets…</div></div>;
  }
  if (!doc) {
    return (
      <div className="settings-section overlays-tab">
        <h3>Overlays</h3>
        {scopeSelector}
        <PresetList presets={presets} userStyles={userStyles} activeName="" sourcePreset="" onSelectPreset={selectPreset} onSelectUserStyle={selectUserStyle}/>
        <div className="overlays-editor-pane field-hint muted" style={{paddingTop:'8px'}}>Select a preset or saved style to start editing.</div>
      </div>
    );
  }

  const el = doc; // non-null from here on

  return (
    <div className="settings-section overlays-tab">
      <h3>Overlays</h3>
      {scopeSelector}
      <div className="overlays-layout">
        <aside className="overlays-presets-col">
          <PresetList presets={presets} userStyles={userStyles} activeName={activeName} sourcePreset={sourcePreset}
                      onSelectPreset={selectPreset} onSelectUserStyle={selectUserStyle}/>
        </aside>

        <section className="overlays-editor-pane">
          <div className="overlay-save-row">
            <input type="text" value={savingAs} onChange={(e)=>setSavingAs(e.target.value)} placeholder="style name…" className="config-input overlay-name-input"/>
            <button className="btn primary" onClick={()=>void save()} disabled={!doc || !savingAs.trim()}>Save as “{savingAs||'name'}”</button>
            {saveMsg && <span className="field-msg ok">{saveMsg}</span>}
            {saveErr && <span className="error">{saveErr}</span>}
          </div>

          <BaseSettings doc={el} patchBase={patchBase}/>

          {scope !== 'global' && (
            <InstalledPacksSection
              logoPacks={logoPacks} onToggleLogoPack={(v)=>void changeLogoPacks(v)}
              availPacks={availPacks} selPacks={selPacks}
              onToggleRichPack={(pn, on) => void toggleRichPack(pn, on)}
              onDeleteRichPack={(pn) => void deleteRichPack(pn)}
              profileName={activeName || savingAs || null}
              packsMsg={packsMsg} packsErr={packsErr}
              sources={sources} rBusySource={rBusySource} rStatus={rStatus} rErr={rErr}
              onDownloadSource={(k, overwrite) => void downloadSource(k, overwrite)}
            />
          )}

          <div className="overlay-subtabs" role="tablist" style={{display:'flex', gap:'4px', margin:'12px 0 8px'}}>
            <button type="button" role="tab" aria-selected={editTab==='elements'}
                    className={`btn ${editTab==='elements' ? 'primary' : ''}`}
                    onClick={()=>setEditTab('elements')}>Elements</button>
            <button type="button" role="tab" aria-selected={editTab==='advanced'}
                    className={`btn ${editTab==='advanced' ? 'primary' : ''}`}
                    onClick={()=>setEditTab('advanced')}>Advanced fine-tuning</button>
          </div>

          {editTab === 'advanced' ? (
            <FormatRulesSection helpResp={helpResp} selectedIds={selectedIds} previews={previews}/>
          ) : (
            <ElementControls doc={el} updateElement={updateElement} setRibbonText={(v)=>patchBase('ribbon_text', v)}
                              updateStatusColor={updateStatusColor}
                              collectionType={scope !== 'global' ? (jfLibs.find((l)=>l.name===scope)?.collection_type ?? null) : null}
                              dateStyle={globalConfig?.status_overlay?.date_style ?? 'relative'}
                              onDateStyleChange={setDateStyle} dateStyleErr={dateStyleErr}/>
          )}

          <fieldset className="overlay-fieldset">
            <legend>Sample data</legend>
            <label className="form-label" style={{marginBottom:0}}>
              <input type="checkbox" checked={sampleData} onChange={(e)=>setSampleData(e.target.checked)}/>
              Show placeholder badges below for values this item doesn't actually have
              (e.g. a 4K badge on a 1080p item), so you can see what each badge looks like.
              Real applies never use placeholders — missing data always means no badge.
            </label>
          </fieldset>

          <PreviewGrid selectedIds={selectedIds} setSelectedIds={setSelectedIds}
                        libraryNames={libraryNames} previewLibrary={previewLibrary} setPreviewLibrary={setPreviewLibrary}
                        previewSearch={previewSearch} setPreviewSearch={setPreviewSearch}
                        items={previewItems} totalItems={previewItemsResp?.total_items ?? 0}
                        hasMore={!!previewItemsResp?.has_more} loadingMore={previewItemsLoadingMore}
                        onLoadMore={loadMorePreviewItems}
                        previews={previews} busy={previewBusy}/>
        </section>
      </div>
    </div>
  );
}

function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'') || 'overlay';
}

// ---------------------------------------------------------------------------
interface PresetListProps {
  presets:PresetEntry[]; userStyles:{name:string;style:any}[]; activeName:string; sourcePreset:string;
  onSelectPreset:(n:string)=>void; onSelectUserStyle:(e:{name:string;style:any})=>void;
}
function PresetList({presets, userStyles, activeName, sourcePreset, onSelectPreset, onSelectUserStyle}: PresetListProps) {
  return (
    <div className="overlays-preset-list">
      <h4>Presets</h4>
      {presets.map((p) => (
        <button key={p.name} type="button"
                className={`preset-row ${sourcePreset===p.name && !activeName?'active':''}`}
                onClick={()=>onSelectPreset(p.name)} title={p.description}>
          <span>{p.name}</span>
        </button>
      ))}
      {userStyles.length > 0 && <>
        <h4 style={{marginTop:'12px'}}>My styles</h4>
        {userStyles.map((u) => (
          <button key={u.name} type="button"
                  className={`preset-row ${activeName===u.name?'active':''}`}
                  onClick={()=>onSelectUserStyle(u)}>
            <span>{u.name}</span>
          </button>
        ))}
      </>}
    </div>
  );
}

// ---------------------------------------------------------------------------
function BaseSettings({doc, patchBase}:{doc:OverlayDoc; patchBase:(k:keyof OverlayDoc,v:any)=>void}) {
  return (
    <fieldset className="overlay-fieldset">
      <legend>Base settings</legend>
      <div className="field-grid-3col">
        <label className="form-label">Anchor
          <select value={doc.base_position} onChange={(e)=>patchBase('base_position', e.target.value)}>
            {ALL_POSITIONS.map(p=><option key={p} value={p}>{p.replace('_',' ')}</option>)}
          </select>
        </label>
        <NumField label="Margin (px)" value={doc.margin} min={0} max={500} onCommit={(v)=>patchBase('margin',v)}/>
        <NumField label="Font size" value={doc.font_size} min={8} max={300} onCommit={(v)=>patchBase('font_size',v)}/>
        <ColorField label="Backdrop" value={doc.bg} allowAlpha onChange={(v)=>patchBase('bg',v)}/>
        <ColorField label="Text color" value={doc.fg} allowAlpha={false} onChange={(v)=>patchBase('fg',v)}/>
        <NumField label="Radius" value={doc.radius} min={0} max={100} onCommit={(v)=>patchBase('radius',v)}/>
      </div>
    </fieldset>
  );
}

// ---------------------------------------------------------------------------
// S9d4c: moved here from the Libraries tab so importing a new Kometa setup
// and assigning it to the profile being edited above happen in one place.
// Download+import a known overlay source by name. Finmeta fetches the files
// itself instead of requiring them already on local disk -- not every
// Kometa-style source works this way (only ones with self-contained
// image+condition overlays, e.g. jmxd; sources built around Kometa's own
// built-in default overlays or Plex-collection matching come back empty, see
// feature.kometa_import help text), so this stays a pick-a-known-source list
// rather than an arbitrary URL box.
// 2026-10-20 operator feedback: "Logo packs" (the global Kometa/custom
// chooser + install button) and the old "Packs" fieldset (per-profile,
// per-category checkboxes) were three overlapping pack-related controls
// scattered across the tab. Merged into one: download/import stays as
// before, immediately followed by one Activate/Deactivate button per
// installed source (Kometa logos, custom flat logos, and any richer
// imported/downloaded pack) -- no more granular per-category toggles.
function KometaLogoRow({active, onToggle}: {active: boolean; onToggle: (on: boolean) => void}) {
  const [status, setStatus] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<{installed:boolean;have:number;total:number;complete:boolean}|null>(null);

  async function refresh(): Promise<void> {
    try { setDetail(await logoPackStatus('kometa')); } catch { /* ignore transient failure */ }
  }
  useEffect(() => { void refresh(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  async function doInstall() {
    setBusy(true); setStatus(detail && detail.installed ? 'Updating…' : 'Installing…');
    try {
      const r = await installLogoPack('kometa');
      setStatus(`Done: ${r.downloaded} new, ${r.skipped} already present${r.failed ? `, ${r.failed} failed` : ''}.`);
      await refresh();
    } catch (e: any) {
      setStatus(e?.message ?? 'install failed');
    } finally { setBusy(false); }
  }

  return (
    <div className="pack-row">
      <label className="pack-name">
        <strong>Kometa logo pack</strong>
        <span className="field-hint muted"> — resolution/audio/rating icons, downloaded from the Kometa project</span>
      </label>
      <div style={{display:'flex', gap:'10px', alignItems:'center', flexWrap:'wrap', marginTop:'4px'}}>
        <button type="button" className={`btn ${active ? '' : 'primary'}`} onClick={() => onToggle(!active)}>
          {active ? 'Deactivate' : 'Activate'}
        </button>
        {detail && !detail.installed && (
          <button type="button" className="btn" onClick={() => void doInstall()} disabled={busy}>Install Kometa logos</button>
        )}
        {detail?.installed && (
          <>
            <span className="field-msg ok">Installed{detail.complete ? '' : ` (${detail.have}/${detail.total})`}</span>
            <button type="button" className="btn" onClick={() => void doInstall()} disabled={busy}>Update</button>
          </>
        )}
        {status && <span className="field-msg">{status}</span>}
      </div>
    </div>
  );
}

function InstalledPacksSection({
  logoPacks, onToggleLogoPack, availPacks, selPacks, onToggleRichPack, onDeleteRichPack, profileName, packsMsg, packsErr,
  sources, rBusySource, rStatus, rErr, onDownloadSource}: {
  logoPacks: 'kometa'[]; onToggleLogoPack: (v: 'kometa'[]) => void;
  availPacks: ImagePackInfo[]; selPacks: string[];
  onToggleRichPack: (pn: string, on: boolean) => void;
  onDeleteRichPack: (pn: string) => void;
  profileName: string | null; packsMsg: string | null; packsErr: string | null;
  sources: Record<string, {label:string; zip_url:string; default_pack_name:string}>;
  rBusySource: string | null; rStatus: string | null; rErr: string | null;
  onDownloadSource: (sourceKey: string, overwrite: boolean) => void;
}) {
  const toggleKometa = (on: boolean) =>
    onToggleLogoPack(on ? ['kometa'] : []);
  // One known source -> one fixed pack name: find which source (if any) a
  // given pack name came from, so its row gets an inline Update button
  // instead of a separate "download" panel elsewhere on the page.
  const sourceKeyByPackName: Record<string, string> = {};
  for (const [key, src] of Object.entries(sources)) sourceKeyByPackName[src.default_pack_name] = key;
  // Was ['ichimoku'] (2026-10-27 -> 2026-10-30): a short-lived download
  // feature for it was tried and reverted (never worked reliably for the
  // operator), and with it the only reason this guard existed -- the
  // operator now wants ichimoku deletable like any other hand-placed pack.
  const PROTECTED_NO_SOURCE = new Set<string>([]);
  const installedPackNames = new Set(availPacks.filter((x) => x.valid).map((x) => x.name));
  const notYetInstalled = Object.entries(sources).filter(([, src]) => !installedPackNames.has(src.default_pack_name));
  return (
    <fieldset className="overlay-fieldset">
      <legend>Installed packs</legend>
      <p className="field-hint muted" style={{marginTop:0}}>
        Activate a pack to use its images for {profileName ? <>profile “{profileName}”</> : <>this profile (save the style above first)</>}.
        Missing images always fall back to plain text badges.
      </p>
      <KometaLogoRow active={logoPacks.includes('kometa')} onToggle={toggleKometa}/>
      {availPacks.filter((x) => !x.valid).map((pk) => (
        <div key={pk.name} className="pack-row">
          <div className="field-msg err">invalid pack “{pk.name}”: {pk.error}</div>
          <button type="button" className="btn btn-ghost" onClick={() => onDeleteRichPack(pk.name)} style={{marginTop:'4px'}}>
            Delete
          </button>
        </div>
      ))}
      {availPacks.filter((x) => x.valid).map((pk) => {
        const checked = selPacks.includes(pk.name);
        const sourceKey = sourceKeyByPackName[pk.name];
        return (
          <div key={pk.name} className="pack-row">
            <label className="pack-name">
              <strong>{pk.name}</strong>
              {pk.description && <span className="field-hint muted"> — {pk.description}</span>}
            </label>
            {pk.missing_images.length > 0 && (
              <div className="pack-missing field-hint" style={{ color: 'var(--update)' }}>
                missing images: {pk.missing_images.join(', ')}
              </div>
            )}
            <div style={{display:'flex', gap:'8px', marginTop:'4px', alignItems:'center', flexWrap:'wrap'}}>
              <button type="button" className={`btn ${checked ? '' : 'primary'}`} disabled={!profileName}
                      onClick={() => onToggleRichPack(pk.name, !checked)}>
                {checked ? 'Deactivate' : 'Activate'}
              </button>
              {sourceKey ? (
                // Known-source pack: same pattern as the Kometa logo pack
                // row below -- no Delete at all, just Activate/Deactivate +
                // Update, so it can't be removed from the UI only to need
                // re-downloading later (operator feedback 2026-10-27).
                <button type="button" className="btn" disabled={rBusySource === sourceKey}
                        onClick={() => onDownloadSource(sourceKey, true)}>
                  {rBusySource === sourceKey ? 'Updating…' : 'Update'}
                </button>
              ) : !PROTECTED_NO_SOURCE.has(pk.name) ? (
                <button type="button" className="btn btn-ghost" onClick={() => onDeleteRichPack(pk.name)}>Delete</button>
              ) : null}
              {sourceKey && rStatus && rBusySource === null && <span className="field-msg ok">{rStatus}</span>}
            </div>
          </div>
        );
      })}
      {notYetInstalled.map(([key, src]) => (
        <div key={key} className="pack-row">
          <label className="pack-name">
            <strong>{src.label}</strong>
            <span className="field-hint muted"> — not installed; other people's images, no licence file, fetched on demand into your own config/assets/packs/ only, never bundled with Finmeta</span>
          </label>
          <div style={{display:'flex', gap:'8px', marginTop:'4px', alignItems:'center'}}>
            <button type="button" className="btn primary" disabled={rBusySource === key}
                    onClick={() => onDownloadSource(key, false)}>
              {rBusySource === key ? 'Downloading…' : 'Download'}
            </button>
            {rStatus && rBusySource === null && <span className="field-msg ok">{rStatus}</span>}
          </div>
        </div>
      ))}
      {rErr && <span className="error">{rErr}</span>}
      {packsMsg && <span className="field-msg ok">{packsMsg}</span>}
      {packsErr && <span className="error">{packsErr}</span>}
    </fieldset>
  );
}

// ---------------------------------------------------------------------------

function ElementControls({doc, updateElement, setRibbonText, updateStatusColor, collectionType,
                          dateStyle, onDateStyleChange, dateStyleErr}:{
  doc:OverlayDoc; updateElement:(k:string,p:any)=>void; setRibbonText:(v:string|null)=>void;
  updateStatusColor:(statusKey:string, color:string|null)=>void;
  collectionType?: string | null;
  dateStyle: string; onDateStyleChange: (v: string) => void; dateStyleErr: string | null;
}) {
  const [openKey, setOpenKey] = useState<string|''>('imdb');
  // Hide elements that can never do anything for this library's type (e.g.
  // "TV status (Sonarr)" on a movies library) instead of showing a dead toggle.
  const visible = ELEMENT_ORDER.filter((key) => {
    if (!TV_ONLY_KEYS.has(key)) return true;
    return !collectionType || collectionType === 'tvshows';
  });
  return (
    <fieldset className="overlay-fieldset">
      <legend>Elements</legend>
      {visible.map((key) => {
        const e = doc.elements[key] ?? {};
        const enabled = e.enabled !== false;
        const isOpen = openKey===key;
        return (
          <div key={key} className={`element-row ${enabled?'':'disabled'}`}
               onClick={()=>setOpenKey(isOpen ? '' : key)} role="button" aria-expanded={isOpen}>
            <span className="element-toggle-label">
              <input type="checkbox" checked={!!e.enabled} onClick={(ev)=>ev.stopPropagation()}
                     onChange={(ev)=>updateElement(key,{enabled:ev.target.checked})}/>
              <span>{ELEMENT_LABELS[key]}</span>
            </span>
            <span className="element-expand-btn" aria-hidden="true">
              {isOpen?'▼':'▸'}
            </span>
            {isOpen && (
              <div className="element-controls-grid" onClick={(ev)=>ev.stopPropagation()}>
                <NineGrid current={e.position || null} basePosition={doc.base_position} onSelect={(pos)=>updateElement(key,{position:pos})}/>

                <NumField label="X offset" value={e.x_offset??0} min={-500} max={500} onCommit={(v)=>updateElement(key,{x_offset:v})}/>
                <NumField label="Y offset" value={e.y_offset??0} min={-500} max={500} onCommit={(v)=>updateElement(key,{y_offset:v})}/>
                <SizeInput value={e.size ?? null} placeholder={`inherit (${doc.font_size}px)`}
                           onCommit={(v)=>updateElement(key,{size:v===null? null : Math.max(8,Math.min(240,v))})}/>
                <ColorField label="Text color" value={e.color??''} allowAlpha onChange={(v)=>updateElement(key,{color:v||undefined})}/>
                <ColorField label="Backdrop" value={e.back_color??''} allowAlpha onChange={(v)=>updateElement(key,{back_color:v||undefined})}/>
                <NullableNumField label="Radius (px)" value={e.radius ?? ''} min={0} max={100}
                                  emptyLabel="(inherit)" onCommit={(v)=>updateElement(key,{radius: v===null? null : v})}/>
                <NumField label="Pad X" value={e.padding_x??10} min={0} max={60} onCommit={(v)=>updateElement(key,{padding_x:v})}/>
                <NumField label="Pad Y" value={e.padding_y??6} min={0} max={60} onCommit={(v)=>updateElement(key,{padding_y:v})}/>
                <RangeSlider label="Opacity" value={e.opacity??1.0} step={0.05} onChange={(v)=>updateElement(key,{opacity:v})}/>

                {(key==='status' || key==='trending_rank') && (
                  <label className="full-width-toggle">
                    <input type="checkbox" checked={e.full_width !== false}
                           onChange={(ev)=>updateElement(key,{full_width:ev.target.checked})}/>
                    <span>Full-width bar <span className="field-hint muted">(uncheck for a pill sized to the text, using Pad X/Y above)</span></span>
                  </label>
                )}

                {key==='ribbon' && (
                  <label className="form-label overlay-ribbon-text">Ribbon text
                    <input type="text" maxLength={120} value={doc.ribbon_text ?? ''} placeholder="(empty = no ribbon)"
                           onChange={(ev)=>setRibbonText(ev.target.value || null)}/>
                  </label>
                )}

                {key==='status' && (
                  <label className="form-label" style={{gridColumn:'span 2'}}>
                    Date style <span className="field-hint muted">(applies everywhere this element is used, not just this profile)</span>
                    <select value={dateStyle} onChange={(ev)=>onDateStyleChange(ev.target.value)}>
                      <option value="relative">relative (TODAY / TOMORROW / weekday / DD.MM)</option>
                      <option value="classic">classic (always DD.MM)</option>
                      <option value="weekday">weekday (always a day name, never a date)</option>
                    </select>
                    {dateStyleErr && <span className="error">{dateStyleErr}</span>}
                  </label>
                )}

                {key==='status' && (
                  <div className="overlay-status-colors" style={{gridColumn:'1 / -1'}}>
                    <p className="form-label" style={{marginBottom:'4px'}}>
                      Per-status ribbon colour (overrides "Backdrop" above for that one status; empty = built-in default)
                    </p>
                    <div className="element-controls-grid">
                      {STATUS_COLOR_KEYS.map(({key:sk, label, builtin}) => (
                        <ColorField key={sk} label={`${label} (default ${builtin})`}
                                    value={(doc.status_colors||{})[sk] ?? ''}
                                    allowAlpha={false}
                                    onChange={(v)=>updateStatusColor(sk, v || null)}/>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </fieldset>
  );
}

// ---------------------------------------------------------------------------
function NineGrid({current, basePosition, onSelect}:{current:string|null; basePosition:string; onSelect:(pos:string)=>void}) {
  const effective = current ?? basePosition;
  return (
    <div className="nine-grid" role="group" aria-label="Position grid">
      {GRID_ROWS.map((row,r) => row.map((col,c) => (
        <button key={col} type="button"
                className={`nine-cell ${effective===col?'active':''}`}
                title={`${r+1}-${c+1} (${col.replace('_',' ')})`}
                onClick={()=>onSelect(col)}></button>
      )))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Format rules (K9): read-only rule list per category with enabled toggle +
// priority editing, saved via the existing config API (overlays.format_rules,
// validated server-side). For the previewed item, shows which rule matched per
// category (from the plan entry) or "no match". Help text comes from /api/help.
interface FormatRuleEntry { key: string; match: string; label: string; image: string; priority: number; enabled: boolean }
const FORMAT_CATEGORIES = [
  { id: 'resolution', title: 'Resolution' },
  { id: 'dynamic_range', title: 'Dynamic range (HDR)' },
  { id: 'audio', title: 'Audio' },
] as const;

function FormatRulesSection({ helpResp, selectedIds, previews }: {
  helpResp: HelpResp | null;
  selectedIds: string[];
  previews: { item_id: string; format_rule_matches?: Record<string, { key: string; label: string } | null> }[];
}) {
  const [rules, setRules] = useState<Record<string, FormatRuleEntry[]>>({});
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  useEffect(() => {
    // Defaults come from the backend endpoint (S9d1) so the UI never drifts
    // from DEFAULT_FORMAT_RULES; stored rules win per category.
    Promise.all([getConfig(), getDefaultFormatRules()]).then(([d, dr]) => {
      const fr = (d.parsed?.overlays?.format_rules as Record<string, FormatRuleEntry[]>) || {};
      setRules(normalizeFormatRules(dr.rules, fr));
    }).catch(() => {});
  }, []);

  function patchRule(cat: string, idx: number, patch: Partial<FormatRuleEntry>) {
    setRules(prev => ({ ...prev, [cat]: prev[cat].map((r, i) => (i === idx ? { ...r, ...patch } : r)) }));
  }

  async function save() {
    setSaveErr(null); setSaveMsg(null);
    try {
      const d = await getConfig();
      let docYaml: any;
      try { docYaml = (yamlLoad(d.yaml) as any) ?? {}; } catch { docYaml = {}; }
      if (!docYaml.overlays || typeof docYaml.overlays !== 'object') docYaml.overlays = {};
      // Persist rules in the stored categories' order; keep other overlays keys intact.
      const ordered: Record<string, FormatRuleEntry[]> = {};
      for (const cat of Object.keys(rules)) {
        ordered[cat] = [...rules[cat]].sort((a, b) => a.priority - b.priority);
      }
      docYaml.overlays.format_rules = ordered;
      await putConfig(yamlDump(docYaml, { noRefs: true, lineWidth: -1 }));
      setSaveMsg('Format rules saved');
      setTimeout(() => setSaveMsg(null), 3500);
    } catch (e: any) {
      setSaveErr(e?.message ?? 'save failed');
    }
  }

  const helpFor = helpResp?.help['overlays.format_rules']?.long;
  // Which rule matched per category for the FIRST selected preview item.
  const firstPreview = selectedIds.length ? previews.find(p => p.item_id === selectedIds[0]) : undefined;

  return (
    <fieldset className="overlay-fieldset format-rules-section">
      <legend>Format rules {helpFor && <HelpIcon help={helpFor}/>}</legend>
      {Object.keys(rules).length === 0 ? (
        <div className="field-hint muted">Loading rules…</div>
      ) : (
        <>
          {FORMAT_CATEGORIES.map(({ id, title }) => {
            const list = rules[id] ?? [];
            const matched = firstPreview?.format_rule_matches?.[id];
            return (
              <div key={id} className="format-rules-category" style={{ marginBottom: '10px' }}>
                <div className="format-rules-cat-head">
                  <strong>{title}</strong>
                  <span className="field-hint muted">
                    {matched ? `matched: ${matched.key} (${matched.label})` : 'no match'}
                  </span>
                </div>
                <table className="format-rules-table">
                  <thead><tr><th>Enabled</th><th>Rule</th><th>Conditions</th><th>Priority</th></tr></thead>
                  <tbody>
                    {list.map((r, i) => (
                      <tr key={r.key} className={matched?.key === r.key ? 'matched' : ''}>
                        <td><input type="checkbox" checked={!!r.enabled} onChange={(e) => patchRule(id, i, { enabled: e.target.checked })}/></td>
                        <td>{r.label} <span className="field-hint muted">({r.key})</span></td>
                        <td><code>{r.match}</code></td>
                        <td><input type="number" min={0} value={r.priority}
                                   onChange={(e) => patchRule(id, i, { priority: Math.max(0, parseInt(e.target.value || '0', 10)) })}/></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })}
          <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
            <button type="button" className="btn primary" onClick={() => void save()}>Save format rules</button>
            {saveMsg && <span className="field-msg ok">{saveMsg}</span>}
            {saveErr && <span className="error">{saveErr}</span>}
          </div>
        </>
      )}
    </fieldset>
  );
}

function normalizeFormatRules(
  defaults: Record<string, FormatRuleEntry[]>,
  stored: Record<string, FormatRuleEntry[]>): Record<string, FormatRuleEntry[]> {
  // Backend defaults (fetched from /api/format-rules/defaults) are the single
  // source of truth; stored rules (if any) win per category.
  const out: Record<string, FormatRuleEntry[]> = {};
  for (const cat of Object.keys(defaults)) {
    const s = stored[cat];
    out[cat] = (s && s.length ? s : defaults[cat]).map(r => ({ ...r }));
  }
  return out;
}



function HelpIcon({ help }: { help?: string }) {
  const [open, setOpen] = useState(false);
  if (!help) return null;
  return (
    <span className="help-icon" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
      (i)
      {open && <span className="help-pop" dangerouslySetInnerHTML={{ __html: help.replace(/\n/g, '<br/>') }} />}
    </span>
  );
}

interface PreviewItemEntry { item_id: string; image?: string; error?: string; samples?: string[] }

function PreviewGrid({selectedIds, setSelectedIds, libraryNames, previewLibrary, setPreviewLibrary,
                       previewSearch, setPreviewSearch, items, totalItems, hasMore, loadingMore, onLoadMore,
                       previews, busy}:{
  selectedIds: string[]; setSelectedIds:(ids:string[])=>void;
  libraryNames: string[]; previewLibrary: string; setPreviewLibrary:(v:string)=>void;
  previewSearch: string; setPreviewSearch:(v:string)=>void;
  items: ItemLightView[]; totalItems: number; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void;
  previews:PreviewItemEntry[]; busy:boolean;
}) {
  const toggleItem = (id:string) => {
    if (selectedIds.includes(id)) setSelectedIds(selectedIds.filter(x=>x!==id));
    else if (selectedIds.length>=4) return; // cap at four per spec
    else setSelectedIds([...selectedIds,id]);
  };
  return (
    <fieldset className="overlay-fieldset">
      <legend>Preview ({selectedIds.length}/4)</legend>
      <div style={{display:'flex',gap:'8px',flexWrap:'wrap',marginBottom:'8px'}}>
        <label className="form-label" style={{flex:'0 0 auto'}}>Library
          <select value={previewLibrary} onChange={(e)=>setPreviewLibrary(e.target.value)}>
            {libraryNames.map(n=><option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        {/* Server-side search (Jellyfin SearchTerm), same as LibraryBrowser.tsx --
            operator-reported (2026-10-30): this used to only filter whichever one
            page had already loaded, so a title outside that page was never found
            no matter what you typed. */}
        <input className="config-input" type="text" style={{flex:'1 1 160px'}}
               placeholder={`Search ${totalItems} title${totalItems===1?'':'s'}…`}
               value={previewSearch} onChange={(e)=>setPreviewSearch(e.target.value)}/>
      </div>
      {items.length === 0 ? (
        <div className="field-hint muted">
          {previewSearch.trim() ? <>No titles match "{previewSearch}".</> : <>No library items available to preview against.</>}
        </div>
      ) : (
        <>
          <div style={{display:'flex',gap:'6px',flexWrap:'wrap',marginBottom:'8px',maxHeight:'220px',overflowY:'auto'}}>
            {items.map((it)=>(
              <button key={it.item_id} type="button"
                      className={`btn preview-item-chip ${selectedIds.includes(it.item_id)?'active':''}`}
                      onClick={()=>toggleItem(it.item_id)} title={`${it.media_type}`}>{trunc(it.item_name,16)}</button>
            ))}
          </div>
          {hasMore && (
            <button type="button" className="btn load-more-btn" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? 'Loading…' : `Load more (${items.length}/${totalItems})`}
            </button>
          )}
          {previews.length===0 && !busy && selectedIds.length>0 && <div className="field-hint muted">Waiting for server render…</div>}
          <div className="preview-grid-2x2">
            {/* Only render cells for items still selected so a server response that
                is one debounce cycle stale never lingers in the 2x2 grid after an
                item is deselected; fewer than four picks fills fewer slots. */}
            {selectedIds.slice(0,4).map((id,i)=>{
              const p = previews.find(q=>q.item_id===id);
              if (!p || !p.image && !p.error) return <div key={'empty'+id+i} className="preview-cell placeholder"><em>slot {i+1}</em></div>;
              const item = items.find(x=>x.item_id===id);
              return (
                <figure key={id} className="preview-cell" style={{margin:0}}>
                  {p.image ? <img src={p.image} alt={item?.item_name ?? id}/> : null}
                  {(p.samples && p.samples.length) ? (
                    <span className="sample-data-tag"
                          title={"synthetic placeholder values shown for: "+(p.samples.join(", "))}>sample data</span>
                  ) : null}
                  <figcaption>{item ? `${item.item_name} (${item.media_type})` : id}{p.error? ` - ${p.error}`:''}{(p.samples&&p.samples.length)?`: samples `+p.samples.join(' '):''}</figcaption>
                </figure>
              );
            })}
          </div>
        </>
      )}
      {busy && <div className="field-hint muted">Rendering preview…</div>}
    </fieldset>
  );
}

function trunc(s:string,n:number){return s.length>n? s.slice(0,n-1)+'…':s;}

// ---- small reusable form widgets ------------------------------------------
function NumField({label,value,min,max,onCommit}:{
  label:string; value:number|null|undefined; min?:number; max?:number; onCommit:(v:number)=>void;
}) {
  const [draft,setDraft] = useState<string>(value==null?'':String(value));
  useEffect(()=>{ setDraft(value==null?'':String(value)); },[value]);
  return (
    <label className="form-label num-field" title={`${min??'−?–'}${max!=null?`–${max}px`:''}`}>
      {label}
      <input type="number" step={1} value={draft} disabled={value==null}
             onChange={(e)=>setDraft(e.target.value)}
             onBlur={()=>{ if(draft.trim()==='') return; let n=Number(draft); if(Number.isNaN(n)) return;
               if(min!=null)n=Math.max(min,n); if(max!=null)n=Math.min(max,n); onCommit(Math.round(n)); }}/>
    </label>
  );
}

function NullableNumField({label,value,min,max,emptyLabel,onCommit}:{
  label:string; value:number|string|null|undefined; min?:number; max?:number; emptyLabel?:string; onCommit:(v:any)=>void;
}) {
  const isNull = value===null || value==='' ;
  const [draft,setDraft] = useState<string>(isNull?'':String(value ?? ''));
  useEffect(()=>{ setDraft(isNull?'':String(value ?? '')); },[value]); // eslint-disable-line react-hooks/exhaustive-deps
  function commit() {
    if (draft.trim()==='') { onCommit(null); return; }
    const n = parseInt(String(draft),10);
    if (Number.isNaN(n)) { onCommit(null); return; }
    let v = n;
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    onCommit(Math.round(v));
  }
  return (
    <label className="form-label num-field">
      {label}
      <input type="text" inputMode="numeric" value={draft} placeholder={emptyLabel ?? '(inherit)'} spellCheck={false}
             onChange={(e)=>setDraft(e.target.value)} onBlur={()=>commit()}/>
    </label>
  );
}

function SizeInput({value,placeholder,onCommit}:{value:number|null; placeholder:string; onCommit:(v:number|null)=>void}) {
  const [draft,setDraft]=useState<string>(value==null?'':String(value));
  useEffect(()=>{ setDraft(value==null?'':String(value)); },[value]);
  return (
    <label className="form-label">Size (px)
      <input type="text" inputMode="numeric" value={draft} placeholder={placeholder} spellCheck={false}
             onChange={(e)=>setDraft(e.target.value)}
             onBlur={()=>{ if(!draft.trim()){onCommit(null);return;} const n=parseInt(draft,10); if(Number.isNaN(n)) onCommit(null); else onCommit(n); }}/>
    </label>
  );
}

const HEX6 = /^#[0-9A-Fa-f]{6}$/;
const HEX8 = /^#[0-9A-Fa-f]{8}$/;
function normalizeHex(raw: string): string {
  let s = raw.trim();
  if (!s) return '';
  if (s[0] !== '#' && /^[0-9A-Fa-f]{6}$/.test(s)) s = '#'+s;
  if (HEX6.test(s) || HEX8.test(s)) return s.toUpperCase();
  return raw; // leave invalid input visible until it becomes valid again
}

function ColorField({label,value,allowAlpha,onChange}:{label:string; value?:string|undefined; allowAlpha:boolean; onChange:(v:string)=>void}) {
  const full = value ?? '';
  const sixDigitForPicker = HEX8.test(full)? full.slice(0,7) : (full ? full : '#ffffff');
  return (
    <label className="form-label color-field" title={allowAlpha? 'Hex #RRGGBB or #RRGGBBAA' : 'Hex #RRGGBB'}>
      {label}
      <div style={{display:'flex',gap:'6px'}}>
        <input type="color" value={sixDigitForPicker} disabled={!allowAlpha&&false} aria-hidden={!allowAlpha} tabIndex={!allowAlpha?-1:0}
               onChange={(e)=>{
                 const hex6 = e.target.value;
                 if (!allowAlpha) { onChange(hex6); }
                 else {
                   const m = full.match(/^#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})$/i);
                   onChange('#'+hex6.replace('#','')+(m && m[2] ? m[2].toUpperCase() : 'FF'));
                 }
               }}/>
        <input type="text" value={full} placeholder="(inherit)" spellCheck={false}
               onChange={(e)=>onChange(normalizeHex(e.target.value))}/>
      </div>
    </label>
  );
}

function RangeSlider({label,value,step,onChange}:{label:string;value:number;step:number;onChange:(v:number)=>void}) {
  return (
    <label className="form-label range-field">
      {label}: {Math.round(value*100)}%
      <input type="range" min={0} max={1} step={step} value={value} onChange={(e)=>onChange(Number(e.target.value))}/>
    </label>
  );
}

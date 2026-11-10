// S14 (2026-11-09): pack management extracted out of OverlaysEditor.tsx's
// internal state into its own shared component -- SPEC_S14_UI.md §4's IA
// table maps "Settings ▸ Overlays ▸ pack management" to a standalone
// **Packs** page. This supersedes the S9d4c decision recorded in
// ProfilesPanel.tsx ("packs moved to the Overlays tab so they sit next to
// the profile they apply to") -- same data, same mutation path, just no
// longer nested inside the 1,486-line overlay style editor. OverlaysEditor
// now renders this same component instead of its own copy, so the classic
// Settings ▸ Overlays tab is unchanged behaviourally (SPEC_S14_UI.md §4b:
// shared, not duplicated).
import { useEffect, useState } from 'react';
import {
  getConfig, putConfig, installLogoPack, logoPackStatus,
  getPacks, deletePack, scaffoldPack,
} from '../api';
import type { ImagePackInfo } from '../api';
import { load as yamlLoad, dump as yamlDump } from 'js-yaml';

interface Props {
  // Which overlay profile pack activation targets. null/empty disables the
  // Activate buttons (nothing selected yet) -- the Kometa logo pack toggle
  // and the pack registry itself (list/create/delete) are unaffected, since
  // those are global (`overlays.logo_packs`), not per-profile.
  profileName: string | null;
}

export default function PacksPanel({ profileName }: Props) {
  const [logoPacks, setLogoPacks] = useState<'kometa'[]>([]);
  const [availPacks, setAvailPacks] = useState<ImagePackInfo[]>([]);
  const [selPacks, setSelPacks] = useState<string[]>([]);
  const [packsMsg, setPacksMsg] = useState<string | null>(null);
  const [packsErr, setPacksErr] = useState<string | null>(null);
  // Real resolved path (operator feedback: a bare "assets/packs/..." reads
  // as somewhere inside the app itself, not the folder actually bind-mounted
  // into the Docker container -- show the real one instead of guessing at
  // a generic placeholder that's wrong for most installs).
  const [packsDir, setPacksDir] = useState<string | null>(null);

  useEffect(() => {
    getConfig()
      .then((d) => {
        const v = (d.parsed?.overlays?.logo_packs as string[])
          ?? (typeof d.parsed?.overlays?.logo_pack === 'string' ? [d.parsed.overlays.logo_pack] : []);
        setLogoPacks((v ?? []).filter((x): x is 'kometa' => x === 'kometa'));
      })
      .catch(() => { /* keep default */ });
  }, []);

  async function refreshPacks(): Promise<void> {
    try {
      const r = await getPacks();
      setAvailPacks(r.packs ?? []);
      setPacksDir(r.packs_dir ?? null);
    } catch { /* keep last good */ }
  }
  useEffect(() => { void refreshPacks(); }, []);

  // Seed the activation checkboxes from whichever profile is now targeted --
  // mirrors OverlaysEditor's old loadPacksFromProfile(), now driven by the
  // profileName prop instead of that component's own scope-change effect.
  useEffect(() => {
    if (!profileName) { setSelPacks([]); return; }
    getConfig()
      .then((d) => {
        const prof = d.parsed?.overlays?.profiles?.[profileName];
        setSelPacks(Array.isArray(prof?.packs) ? [...prof.packs] : []);
      })
      .catch(() => setSelPacks([]));
  }, [profileName]);

  async function scaffoldNewPack(name: string, categories: string[]): Promise<void> {
    setPacksErr(null); setPacksMsg(null);
    try {
      await scaffoldPack(name, categories);
      await refreshPacks();
      setPacksMsg(`"${name}" created — drop images into ${packsDir ?? 'assets/packs'}/${name}/images/<category>/`);
      setTimeout(() => setPacksMsg(null), 6000);
    } catch (e: any) {
      setPacksErr(e?.message ?? 'create failed');
    }
  }

  // Persisted immediately (no separate save step). Reads the FULL config,
  // mutates only this profile's `packs` array, writes the full config back
  // -- the exact pattern the 2026-10-19 data-loss bug violated (that one
  // wrote `{style: doc}` alone and silently dropped `elements`); never
  // replace `prof` wholesale here.
  async function toggleRichPack(pn: string, on: boolean): Promise<void> {
    setPacksErr(null); setPacksMsg(null);
    try {
      const d = await getConfig();
      let cfg: any; try { cfg = (yamlLoad(d.yaml) as any) ?? {}; } catch { cfg = {}; }
      if (!cfg.overlays || typeof cfg.overlays !== 'object') cfg.overlays = {};
      if (!cfg.overlays.profiles || typeof cfg.overlays.profiles !== 'object') cfg.overlays.profiles = {};
      const prof = cfg.overlays.profiles[profileName as string];
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
    } catch { /* leave selection unchanged on failure */ }
  }

  const toggleKometa = (on: boolean) => void changeLogoPacks(on ? ['kometa'] : []);

  return (
    <fieldset className="overlay-fieldset">
      <legend>Installed packs</legend>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Activate a pack to use its images for {profileName ? <>profile “{profileName}”</> : <>a profile (pick one above)</>}.
        Missing images always fall back to plain text badges.
      </p>
      <KometaLogoRow active={logoPacks.includes('kometa')} onToggle={toggleKometa} />
      {availPacks.filter((x) => !x.valid).map((pk) => (
        <div key={pk.name} className="pack-row">
          <div className="field-msg err">invalid pack “{pk.name}”: {pk.error}</div>
          <button type="button" className="btn btn-ghost" onClick={() => void deleteRichPack(pk.name)} style={{ marginTop: '4px' }}>
            Delete
          </button>
        </div>
      ))}
      {availPacks.filter((x) => x.valid).map((pk) => {
        const checked = selPacks.includes(pk.name);
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
            {pk.warnings?.length > 0 && (
              <div className="pack-missing field-hint" style={{ color: 'var(--update)' }}>
                {pk.warnings.join(' · ')}
              </div>
            )}
            <div style={{ display: 'flex', gap: '8px', marginTop: '4px', alignItems: 'center', flexWrap: 'wrap' }}>
              <button type="button" className={`btn ${checked ? '' : 'primary'}`} disabled={!profileName}
                      onClick={() => void toggleRichPack(pk.name, !checked)}>
                {checked ? 'Deactivate' : 'Activate'}
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => void deleteRichPack(pk.name)}>Delete</button>
            </div>
          </div>
        );
      })}
      <NewPackForm packsDir={packsDir} onCreate={(name, cats) => void scaffoldNewPack(name, cats)} />
      <PackNamingHelp packsDir={packsDir} />
      {packsMsg && <span className="field-msg ok">{packsMsg}</span>}
      {packsErr && <span className="error">{packsErr}</span>}
    </fieldset>
  );
}

// 2026-10-20: resolution/audio/rating icon set downloaded on demand from the
// Kometa project (trademark logos -- never shipped with Finmeta, never
// committed to git). Global (`overlays.logo_packs`), not per-profile --
// activation here applies everywhere, regardless of which profile the
// surrounding page happens to be showing.
function KometaLogoRow({ active, onToggle }: { active: boolean; onToggle: (on: boolean) => void }) {
  const [status, setStatus] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<{ installed: boolean; have: number; total: number; complete: boolean } | null>(null);

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
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap', marginTop: '4px' }}>
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

const SCAFFOLD_CATEGORIES: { id: string; label: string; exampleKey: string }[] = [
  { id: 'resolution', label: 'Resolution', exampleKey: 'res_4k' },
  { id: 'dynamic_range', label: 'Dynamic range (HDR)', exampleKey: 'hdr10' },
  { id: 'audio', label: 'Audio', exampleKey: 'dts' },
];

// 2026-11-01: the only way to start a new pack now that Kometa overlay-import
// is gone -- writes pack.yml pre-filled with K9's own format_rule keys (see
// docs/BADGE_PACKS.md), the operator then drops images into the folders it
// creates. Own small form, not a modal -- matches this fieldset's existing
// inline-row style.
function NewPackForm({ packsDir, onCreate }: { packsDir: string | null; onCreate: (name: string, categories: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [cats, setCats] = useState<string[]>(SCAFFOLD_CATEGORIES.map((c) => c.id));
  const dir = packsDir ?? 'assets/packs';

  if (!open) {
    return (
      <div style={{ marginTop: '6px' }}>
        <button type="button" className="btn btn-ghost" onClick={() => setOpen(true)}>
          + New pack
        </button>
        <p className="field-hint muted" style={{ margin: '4px 0 0' }}>
          Tip: for a resolution/HDR/audio-only pack you can skip this form entirely — create{' '}
          <code>{dir}/&lt;name&gt;/images/</code> yourself and drop correctly-named PNGs
          straight into it, no pack.yml or subfolders needed. See docs/BADGE_PACKS.md.
        </p>
      </div>
    );
  }
  const valid = /^[A-Za-z0-9_.-]+$/.test(name.trim());
  return (
    <div className="pack-row" style={{ border: '1px dashed var(--border)', padding: '10px' }}>
      <label className="form-label">Pack name
        <input type="text" value={name} placeholder="my-badges"
               onChange={(e) => setName(e.target.value)} className="config-input" />
      </label>
      <div style={{ display: 'flex', gap: '14px', margin: '8px 0', flexWrap: 'wrap' }}>
        {SCAFFOLD_CATEGORIES.map((c) => (
          <label key={c.id} style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
            <input type="checkbox" checked={cats.includes(c.id)}
                   onChange={(e) => setCats(e.target.checked
                     ? [...cats, c.id] : cats.filter((x) => x !== c.id))} />
            {c.label} <code className="field-hint muted">({c.id})</code>
          </label>
        ))}
      </div>
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <button type="button" className="btn primary" disabled={!valid || cats.length === 0}
                onClick={() => { onCreate(name.trim(), cats); setOpen(false); setName(''); }}>
          Create
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <div className="field-hint muted" style={{ marginBottom: 0 }}>
        <p style={{ margin: '0 0 4px' }}>
          Creates an empty pack.yml with the right keys pre-filled. Then drop <strong>PNG</strong> files
          (not SVG — the renderer only reads PNG) into the exact folder named in parentheses above, using
          the exact key names from docs/BADGE_PACKS.md, e.g.:
        </p>
        {cats.length > 0 ? (
          <ul style={{ margin: 0, paddingLeft: '18px' }}>
            {SCAFFOLD_CATEGORIES.filter((c) => cats.includes(c.id)).map((c) => (
              <li key={c.id}>
                <code>{dir}/{name.trim() || '…'}/images/{c.id}/{c.exampleKey}.png</code>
              </li>
            ))}
          </ul>
        ) : <p style={{ margin: 0 }}>(pick at least one category above to see example paths)</p>}
      </div>
    </div>
  );
}

// 2026-11-01: every key format_rule actually matches on, with a plain-
// language description, grouped the same way images/<category>/ is on
// disk. The single most-requested piece of info once an operator starts
// dropping their own PNGs in -- "what do I name this file, exactly."
const KEY_REFERENCE: { category: string; label: string; rows: { key: string; means: string }[] }[] = [
  { category: 'resolution', label: 'Resolution', rows: [
    { key: 'res_4k', means: '4K / UHD (source width ≥ 3200px)' },
    { key: 'res_1080p', means: '1080p / Full HD' },
    { key: 'res_720p', means: '720p / HD' },
    { key: 'res_576p', means: '576p (PAL SD)' },
    { key: 'res_480p', means: '480p (NTSC SD)' },
  ] },
  { category: 'dynamic_range', label: 'Dynamic range (HDR)', rows: [
    { key: 'dv_hdr_plus', means: 'Dolby Vision + HDR10+' },
    { key: 'dv_hdr', means: 'Dolby Vision + HDR10' },
    { key: 'dv', means: 'Dolby Vision (no HDR10 layer)' },
    { key: 'hdr10plus', means: 'HDR10+' },
    { key: 'hdr10', means: 'HDR10' },
    { key: 'hlg', means: 'HLG (Hybrid Log-Gamma)' },
  ] },
  { category: 'audio', label: 'Audio', rows: [
    { key: 'truehd_atmos', means: 'Dolby TrueHD with Atmos' },
    { key: 'ddplus_atmos', means: 'Dolby Digital+ (E-AC3) with Atmos' },
    { key: 'dtsx', means: 'DTS:X' },
    { key: 'dtshd_ma', means: 'DTS-HD Master Audio' },
    { key: 'truehd', means: 'Dolby TrueHD' },
    { key: 'ddplus', means: 'Dolby Digital+ (E-AC3)' },
    { key: 'dts', means: 'DTS (plain, no X/HD MA)' },
    { key: 'dd', means: 'Dolby Digital (AC3)' },
    { key: 'aac', means: 'AAC' },
  ] },
];

// Collapsible <details> (native disclosure widget, no new component library
// needed) -- operator-requested: a reference for exactly what to name every
// single image, not just the mechanism, surfaced in the app itself rather
// than only in docs/BADGE_PACKS.md (which still has the advanced
// matchers/full schema).
function PackNamingHelp({ packsDir }: { packsDir: string | null }) {
  const dir = packsDir ?? 'assets/packs';
  return (
    <details className="pack-row" style={{ marginTop: '10px' }}>
      <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
        How do I add my own badge images?
      </summary>
      <div style={{ marginTop: '10px' }}>
        <p className="field-hint muted" style={{ marginTop: 0 }}>
          Create <code>{dir}/&lt;your-pack-name&gt;/images/</code> and drop PNG files
          straight into it — one file per key below, named exactly as shown (no subfolders, no
          pack.yml). Finmeta reads each filename and works out on its own which badge it is;
          every key is unique across all three categories, so the name alone is enough. Add only
          the keys you want to override — anything you skip just has no badge for that case.
          {packsDir && (
            <> This is the folder you bind-mounted as your Finmeta config volume in Docker — not
              a folder inside the app itself.</>
          )}
        </p>
        <p className="field-hint muted">
          <strong>Format:</strong> PNG with a transparent background, tightly cropped to the
          visible artwork (not a full poster-sized canvas) — roughly 150–250px tall is a good
          native size. Finmeta always scales to fit, so exact width doesn't matter.
        </p>
        {KEY_REFERENCE.map((group) => (
          <div key={group.category} style={{ marginTop: '10px' }}>
            <div style={{ fontWeight: 600, fontSize: '0.9em' }}>
              {group.label} <code className="field-hint muted">({group.category})</code>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.88em', marginTop: '4px' }}>
                <tbody>
                  {group.rows.map((r) => (
                    <tr key={r.key} style={{ borderTop: '1px solid var(--border)' }}>
                      <td style={{ padding: '3px 8px 3px 0', whiteSpace: 'nowrap' }}>
                        <code>{r.key}.png</code>
                      </td>
                      <td style={{ padding: '3px 0', color: 'var(--text-muted, inherit)' }}>{r.means}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
        <p className="field-hint muted" style={{ marginTop: '10px', marginBottom: 0 }}>
          Want badges keyed off the video <em>filename</em> instead (scene-release tags like
          "2160p", "DTS-X"), your own categories, or TMDb-rating tiers? Those need a hand-written
          <code> pack.yml</code> — see <code>docs/BADGE_PACKS.md</code> in the repo for the full
          schema and every matcher type.
        </p>
      </div>
    </details>
  );
}

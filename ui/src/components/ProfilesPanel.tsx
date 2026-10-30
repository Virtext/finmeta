// S9d2: library overlay profiles — list/create/rename/delete, per-library
// assignment (libraries from GET /api/jellyfin/libraries), raw YAML edit with
// live server-side validation. All saves go through PUT /api/config so the
// existing last-good/undo semantics apply exactly once.
//
// S9d4c: packs + Kometa import moved to the Overlays tab (OverlaysEditor.tsx)
// so they sit next to the profile they apply to instead of a separate tab.
import { useCallback, useEffect, useState } from 'react';
import { getJellyfinLibraries, getConfig, putConfig } from '../api';
import type { JfLibrary } from '../api';
import * as YAML from 'js-yaml';

interface Props { onSaved?: () => void; onEditInOverlays?: (lib: string) => void }

// config/models.py MovieArtwork / TvArtwork field sets -- all default false.
const MOVIE_ARTWORK_KINDS = ['poster', 'backdrop', 'logo', 'landscape'] as const;
const TV_ARTWORK_KINDS = ['poster', 'backdrop', 'logo', 'season_posters', 'episode_thumbs'] as const;

export default function ProfilesPanel({ onSaved, onEditInOverlays }: Props) {
  const [jlibs, setJlibs] = useState<JfLibrary[]>([]);
  const [profiles, setProfiles] = useState<Record<string, any>>({});
  const [assignments, setAssignments] = useState<Record<string, string | null>>({});
  // S9d4a: per-library artwork_selection override (null = inherit global).
  const [artSel, setArtSel] = useState<Record<string, any>>({});
  // Which artwork KINDS (poster/backdrop/logo/...) this library writes --
  // libraries.<lib>.artwork.<kind>, all false by default (SPEC: "off by
  // default"). There was previously no UI control for this at all; only
  // artwork_selection (which candidate wins) had one, which is moot with
  // every kind off -- reported live as "nothing shows, not even a poster"
  // for a library whose kinds were in fact all still false.
  const [artworkKinds, setArtworkKinds] = useState<Record<string, Record<string, boolean>>>({});
  // S9d6: per-library scheduled poster rotation (always present on the
  // model with enabled=false, so no inherit/null case here).
  const [posterRotation, setPosterRotationState] = useState<Record<string, any>>({});
  // Library names present in config.yml that no longer exist on the live
  // Jellyfin server (e.g. a stale example from an old config.template.yml,
  // or a library actually renamed/removed in Jellyfin) -- surfaced so they
  // can be cleaned up instead of lingering as dead tabs/entries forever.
  const [orphanedLibs, setOrphanedLibs] = useState<string[]>([]);
  const [newName, setNewName] = useState('');
  // S9d2-fix2 step 1: raw-YAML profile editing moved to the Overlays tab; this
  // panel keeps create/rename/delete + per-library dropdown assignment.
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([getJellyfinLibraries(), getConfig()]);
      setJlibs(d.libraries ?? []);
      setProfiles((c.parsed?.overlays?.profiles as Record<string, any>) ?? {});
      const a: Record<string, string | null> = {};
      for (const l of d.libraries) a[l.name] = l.overlay_profile ?? null;
      setAssignments(a);
      // S9d4a: per-library artwork_selection overrides (inherit when unset).
      const asel: Record<string, any> = {};
      for (const [n, lib] of Object.entries<any>(c.parsed?.libraries ?? {})) {
        asel[n] = lib?.artwork_selection ?? null;
      }
      setArtSel(asel);
      // Which artwork kinds (poster/backdrop/logo/...) each library writes.
      const ak: Record<string, Record<string, boolean>> = {};
      for (const [n, lib] of Object.entries<any>(c.parsed?.libraries ?? {})) {
        ak[n] = (lib?.artwork && typeof lib.artwork === 'object') ? lib.artwork : {};
      }
      setArtworkKinds(ak);
      // S9d6: per-library poster rotation (default off, 5 items, Sun 04:00).
      const pr: Record<string, any> = {};
      for (const [n, lib] of Object.entries<any>(c.parsed?.libraries ?? {})) {
        pr[n] = lib?.poster_rotation ?? { enabled: false, item_count: 5, cron: '0 4 * * 0' };
      }
      setPosterRotationState(pr);
      const jfNames = new Set(d.libraries.map((l) => l.name));
      setOrphanedLibs(Object.keys(c.parsed?.libraries ?? {}).filter((n) => !jfNames.has(n)));
    } catch { /* keep last good state */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function flash(m: string) { setMsg(m); setTimeout(() => setMsg(null), 3500); }

  async function saveConfigWith(mutate: (doc: any) => void): Promise<boolean> {
    setBusy(true); setSaveErr(null);
    try {
      const d = await getConfig();
      let doc: any;
      try { doc = (YAML.load(d.yaml) as any) ?? {}; } catch { doc = {}; }
      mutate(doc);
      const text = YAML.dump(doc, { noRefs: true, lineWidth: -1 });
      await putConfig(text);               // server-side validated; 400 on error
      await load();
      onSaved?.();
      return true;
    } catch (e: any) {
      setSaveErr(e?.message ?? 'save failed');
      return false;
    } finally { setBusy(false); }
  }

  async function createProfile() {
    const name = newName.trim().toLowerCase().replace(/\s+/g, '-');
    if (!name) return;
    const ok = await saveConfigWith((doc) => {
      if (!doc.overlays || typeof doc.overlays !== 'object') doc.overlays = {};
      if (!doc.overlays.profiles || typeof doc.overlays.profiles !== 'object') doc.overlays.profiles = {};
      if (doc.overlays.profiles[name]) throw new Error(`profile “${name}” already exists`);
      doc.overlays.profiles[name] = { elements: ['resolution', 'hdr', 'audio'] };
    });
    if (ok) { setNewName(''); flash(`Profile “${name}” created`); }
  }

  async function renameProfile(oldName: string) {
    const next = window.prompt('Rename profile to:', oldName);
    if (!next || next === oldName) return;
    const name = next.trim().toLowerCase().replace(/\s+/g, '-');
    if (!name) return;
    const ok = await saveConfigWith((doc) => {
      const profs = doc.overlays?.profiles ?? {};
      if (!(oldName in profs)) throw new Error(`no profile “${oldName}”`);
      if (name in profs && name !== oldName) throw new Error(`profile “${name}” already exists`);
      profs[name] = profs[oldName];
      delete profs[oldName];
      for (const lib of Object.values<any>(doc.libraries ?? {})) {
        if (lib?.overlay_profile === oldName) lib.overlay_profile = name;
      }
    });
    if (ok) flash(`Renamed to “${name}”`);
  }

  async function deleteProfile(name: string) {
    const usedBy = jlibs.filter((l) => assignments[l.name] === name).map((l) => l.name);
    if (usedBy.length && !window.confirm(`“${name}” is assigned to ${usedBy.join(', ')}. Delete anyway?`)) return;
    if (!window.confirm(`Delete profile “${name}”?`)) return;
    const ok = await saveConfigWith((doc) => {
      const profs = doc.overlays?.profiles ?? {};
      delete profs[name];
      for (const lib of Object.values<any>(doc.libraries ?? {})) {
        if (lib?.overlay_profile === name) {
          lib.overlay_profile = null;
          // Also clear the deprecated `overlays` list, not just
          // overlay_profile -- config.models' auto-migration
          // (_migrate_overlays_list_to_auto_profile) recreates a
          // "<library>-auto" profile from that list on every config load
          // whenever it's non-empty and overlay_profile is unset, which
          // silently resurrected the profile this just "deleted" the
          // instant the UI reloaded config (reported live: Movies-auto/
          // Shows-auto could never actually be deleted).
          if (lib.overlays) lib.overlays = [];
        }
      }
    });
    if (ok) flash(`Profile “${name}” deleted`);
  }

  // S9d4c Fase A step 3: "Managed by Finmeta" master switch per library.
  async function setEnabled(libName: string, enabled: boolean) {
    const ok = await saveConfigWith((doc) => {
      const lib = doc.libraries?.[libName];
      if (!lib) throw new Error(`library “${libName}” not in config — add it in Advanced first`);
      lib.enabled = enabled;
    });
    if (ok) flash(`“${libName}” ${enabled ? 'enabled' : 'disabled'}`);
  }

  async function assign(libName: string, profile: string | null) {
    const ok = await saveConfigWith((doc) => {
      const lib = doc.libraries?.[libName];
      if (!lib) throw new Error(`library “${libName}” not in config — add it in Advanced first`);
      lib.overlay_profile = profile;
    });
    if (ok) flash(profile ? `“${libName}” → ${profile}` : `“${libName}” profile cleared`);
  }

  // S9d4a: set/clear a library's artwork_selection override.
  async function setArtworkSelection(libName: string, value: any) {
    const ok = await saveConfigWith((doc) => {
      const lib = doc.libraries?.[libName];
      if (!lib) throw new Error(`library “${libName}” not in config — add it in Advanced first`);
      if (value === null) delete lib.artwork_selection;
      else lib.artwork_selection = value;
    });
    if (ok) flash(value ? `“${libName}” artwork selection updated` : `“${libName}” inherits global artwork selection`);
  }

  // S9d6: merge-patch a library's poster_rotation block.
  async function setPosterRotation(libName: string, patch: Record<string, any>) {
    const ok = await saveConfigWith((doc) => {
      const lib = doc.libraries?.[libName];
      if (!lib) throw new Error(`library “${libName}” not in config — add it in Advanced first`);
      lib.poster_rotation = { ...(lib.poster_rotation ?? {}), ...patch };
    });
    if (ok) flash(`“${libName}” poster rotation updated`);
  }

  // Toggle one artwork kind (libraries.<lib>.artwork.<kind>) on/off. All
  // kinds default to false (SPEC: "off by default") -- this is the only
  // control for it anywhere in the app.
  async function toggleArtworkKind(libName: string, kind: string, value: boolean) {
    const ok = await saveConfigWith((doc) => {
      const lib = doc.libraries?.[libName];
      if (!lib) throw new Error(`library “${libName}” not in config — add it in Advanced first`);
      if (!lib.artwork || typeof lib.artwork !== 'object') lib.artwork = {};
      lib.artwork[kind] = value;
    });
    if (ok) flash(`“${libName}” ${kind} → ${value ? 'on' : 'off'}`);
  }

  // Remove a stale config.yml library entry entirely -- not just disable it.
  // Cascades to collections/playlists/schedules that reference it, since
  // Config._check_references rejects a save that leaves any of those
  // pointing at a library no longer in `libraries:` (a real config.yml left
  // over from an old config.template.yml example had exactly this: three
  // collections, a playlist and a schedule all still naming the stale
  // library, which would otherwise block the library's own removal with a
  // confusing validation error). Only touches Finmeta's own config -- never
  // anything in Jellyfin or on disk.
  async function removeLibraryEntirely(libName: string) {
    if (!window.confirm(
      `Remove “${libName}” from config.yml entirely (not just disable it)? `
      + `Any collections/playlists/schedules that reference it are removed too. `
      + `Nothing in Jellyfin or on disk is touched.`)) return;
    const ok = await saveConfigWith((doc) => {
      if (doc.libraries) delete doc.libraries[libName];
      if (Array.isArray(doc.collections)) {
        doc.collections = doc.collections.filter((c: any) => c?.library !== libName);
      }
      if (Array.isArray(doc.playlists)) {
        doc.playlists = doc.playlists.filter((p: any) => p?.library !== libName);
      }
      if (Array.isArray(doc.schedules)) {
        doc.schedules = doc.schedules
          .map((s: any) => (Array.isArray(s?.libraries)
            ? { ...s, libraries: s.libraries.filter((l: string) => l !== libName) }
            : s))
          .filter((s: any) => !Array.isArray(s?.libraries) || s.libraries.length > 0);
      }
    });
    if (ok) flash(`“${libName}” removed from config`);
  }

  const names = Object.keys(profiles).sort();

  return (
    <div className="profiles-panel">
      <h3>Overlay profiles</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        A profile bundles an overlay element list plus per-element style and format-rule overrides.
        Assign one per library below; the plan/apply uses it for every item in that library.
      </p>

      <div className="profiles-create-row">
        <input type="text" value={newName} placeholder="new profile name…"
               className="config-input" onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') void createProfile(); }} />
        <button className="btn primary" onClick={() => void createProfile()} disabled={busy || !newName.trim()}>Create</button>
      </div>

      <table className="profiles-table">
        <thead><tr><th>Profile</th><th>Elements</th><th>Used by</th><th></th></tr></thead>
        <tbody>
          {names.length === 0 && (
            <tr><td colSpan={4} className="field-hint muted">No profiles yet — create one above.</td></tr>
          )}
          {names.map((n) => {
            const els: string[] = Array.isArray(profiles[n]?.elements) ? profiles[n].elements : [];
            const usedBy = jlibs.filter((l) => assignments[l.name] === n).map((l) => l.name);
            return (
              <tr key={n}>
                <td>{n}</td>
                <td><span className="field-hint muted">{els.length ? els.join(', ') : '(inherit library overlays)'}</span></td>
                <td><span className="field-hint muted">{usedBy.length ? usedBy.join(', ') : '—'}</span></td>
                <td className="profiles-actions">
                  <span className="field-hint muted">style, elements &amp; packs: Overlays tab</span>
                  <button className="btn" onClick={() => void renameProfile(n)}>Rename</button>
                  <button className="btn danger" onClick={() => void deleteProfile(n)}>Delete</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h3>Library assignment</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        One profile per library ({'`'}libraries.&lt;lib&gt;.overlay_profile{'`'}). The dropdown is the only
        way to assign or clear it here; fine-grained overlay editing lives in the Overlays tab.
      </p>
      <table className="profiles-table">
        <thead><tr><th>Library (Jellyfin)</th><th>Managed by Finmeta</th><th>Profile</th><th>Active elements</th><th></th></tr></thead>
        <tbody>
          {jlibs.map((l) => {
            const profName = assignments[l.name];
            const els: string[] = profName && Array.isArray(profiles[profName]?.elements) ? profiles[profName].elements : [];
            const zeroWarning = !!profName && els.length === 0;
            return (
              <tr key={l.id}>
                <td>{l.name} <span className="field-hint muted">({l.collection_type})</span></td>
                <td>
                  <label className="pack-cat"
                         title={l.configured ? 'Turn this library on/off for Finmeta (master switch)' : 'Add this library in the Advanced tab first'}>
                    <input type="checkbox" disabled={!l.configured || busy} checked={!!l.enabled}
                           onChange={(e) => void setEnabled(l.name, e.target.checked)} />
                    {l.configured ? (l.enabled ? 'enabled' : 'disabled') : 'not in config'}
                  </label>
                </td>
                <td>
                  <select
                    value={assignments[l.name] ?? ''}
                    disabled={!l.configured || busy}
                    onChange={(e) => void assign(l.name, e.target.value || null)}
                    title={l.configured ? 'Assign a profile to this library' : 'Add this library in the Advanced tab first'}>
                    <option value="">(none)</option>
                    {names.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </td>
                <td>
                  {profName ? (
                    <span className={zeroWarning ? 'field-msg err' : 'field-hint muted'}>
                      {els.length} element{els.length === 1 ? '' : 's'}
                      {zeroWarning && ' — warning: profile has 0 active elements, no overlays will render'}
                    </span>
                  ) : (
                    <span className="field-hint muted">—</span>
                  )}
                </td>
                <td>
                  {onEditInOverlays && (
                    <button className="btn" onClick={() => onEditInOverlays(l.name)}>Edit in Overlays</button>
                  )}
                </td>
              </tr>
            );
          })}
          {jlibs.length === 0 && (
            <tr><td colSpan={5} className="field-hint muted">No Jellyfin libraries reachable.</td></tr>
          )}
        </tbody>
      </table>

      {/* Which artwork kinds get written -- all off by default, previously
          no UI for this anywhere (only how a candidate is picked and how
          it's written, both moot with every kind off). */}
      <h3>Artwork kinds</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Which image files Finmeta writes for this library ({'`'}libraries.&lt;lib&gt;.artwork.&lt;kind&gt;{'`'}).
        Every kind is off by default — nothing (not even a poster) is written until you turn one on here.
      </p>
      <table className="profiles-table">
        <thead><tr><th>Library</th><th>Kinds</th></tr></thead>
        <tbody>
          {jlibs.map((l) => {
            const kinds = l.collection_type === 'tvshows' ? TV_ARTWORK_KINDS : MOVIE_ARTWORK_KINDS;
            const current = l.configured ? (artworkKinds[l.name] ?? {}) : {};
            return (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td>
                  {kinds.map((kind) => (
                    <label key={kind} className="pack-cat"
                           title={l.configured ? `Write ${kind} for this library` : 'Add this library in the Advanced tab first'}>
                      <input type="checkbox" disabled={!l.configured || busy}
                             checked={!!current[kind]}
                             onChange={(e) => void toggleArtworkKind(l.name, kind, e.target.checked)} />
                      {kind}
                    </label>
                  ))}
                </td>
              </tr>
            );
          })}
          {jlibs.length === 0 && (
            <tr><td colSpan={2} className="field-hint muted">No Jellyfin libraries reachable.</td></tr>
          )}
        </tbody>
      </table>

      {/* S9d4a: per-library artwork selection preset + N (inherit = global). */}
      <h3>Artwork selection</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Per-library override of the global preset ({'`'}libraries.&lt;lib&gt;.artwork_selection{'`'}).
        “inherit” uses the General tab value.
      </p>
      <table className="profiles-table">
        <thead><tr><th>Library</th><th>Preset</th><th>N (top_n)</th></tr></thead>
        <tbody>
          {jlibs.map((l) => {
            const ov = l.configured ? artSel[l.name] : null;
            return (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td>
                  <select
                    disabled={!l.configured || busy}
                    value={ov?.preset ?? 'inherit'}
                    onChange={(e) => {
                      const v = e.target.value;
                      void setArtworkSelection(l.name,
                        v === 'inherit' ? null : { preset: v, top_n: ov?.top_n ?? 3 });
                    }}
                    title={l.configured ? 'Override the global artwork selection preset' : 'Add this library in the Advanced tab first'}>
                    <option value="inherit">inherit</option>
                    <option value="best_rated">best_rated</option>
                    <option value="random_top_n">random_top_n</option>
                  </select>
                </td>
                <td>
                  <input
                    className="config-input" type="number" min={1} max={20}
                    disabled={!l.configured || busy || !ov}
                    value={ov?.top_n ?? ''}
                    placeholder={ov ? '' : 'inherit'}
                    onChange={(e) => {
                      const n = Math.max(1, Number(e.target.value) || 1);
                      void setArtworkSelection(l.name, { preset: ov.preset, top_n: n });
                    }}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* S9d6: scheduled poster rotation per library (off by default). */}
      <h3>Poster rotation</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Reshuffle and reapply the poster of this library's top-rated items on a schedule
        ({'`'}libraries.&lt;lib&gt;.poster_rotation{'`'}), so the best items don't stay static
        forever. Turning this on forces artwork selection to {'`'}random_top_n{'`'} (with at
        least 2 candidates) for the library above — otherwise there is nothing to rotate between.
        Requires the {'`'}poster{'`'} artwork kind to be on and {'`'}dry_run{'`'} off.
      </p>
      <table className="profiles-table">
        <thead><tr><th>Library</th><th>Enabled</th><th>Top N items</th><th>Cron</th></tr></thead>
        <tbody>
          {jlibs.map((l) => {
            const pr = l.configured ? (posterRotation[l.name] ?? { enabled: false, item_count: 5, cron: '0 4 * * 0' }) : { enabled: false };
            return (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td>
                  <input type="checkbox" disabled={!l.configured || busy}
                         checked={!!pr.enabled}
                         onChange={(e) => void setPosterRotation(l.name, { enabled: e.target.checked })} />
                </td>
                <td>
                  <input
                    className="config-input" type="number" min={1} max={50}
                    disabled={!l.configured || busy || !pr.enabled}
                    value={pr.item_count ?? 5}
                    onChange={(e) => void setPosterRotation(l.name, { item_count: Math.max(1, Number(e.target.value) || 1) })}
                  />
                </td>
                <td>
                  <input
                    className="config-input" type="text"
                    disabled={!l.configured || busy || !pr.enabled}
                    value={pr.cron ?? '0 4 * * 0'}
                    title="5-field cron expression, e.g. `0 4 * * 0` (Sundays 04:00)"
                    onChange={(e) => void setPosterRotation(l.name, { cron: e.target.value })}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {orphanedLibs.length > 0 && (
        <>
          <h3>Stale config entries</h3>
          <p className="field-hint muted" style={{ marginTop: 0 }}>
            These library names are in config.yml but don’t match any library currently on your
            Jellyfin server (e.g. renamed/removed there, or left over from an old default config).
            They still show up as tabs/entries around the app with nothing real behind them.
            Disabling isn’t enough to clear that — remove the entry entirely.
          </p>
          <table className="profiles-table">
            <thead><tr><th>Library</th><th></th></tr></thead>
            <tbody>
              {orphanedLibs.map((name) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td><button className="btn danger" onClick={() => void removeLibraryEntirely(name)}>Remove entirely</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {msg && <div className="field-msg ok">{msg}</div>}
      {saveErr && <div className="field-msg err">{saveErr}</div>}
    </div>
  );
}

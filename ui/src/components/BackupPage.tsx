import { useCallback, useEffect, useRef, useState } from 'react';
import {
  backupExportUrl, deleteBundle, getBundles, getHelp, inspectBundleFile,
  inspectStoredBundle, restoreBundleFile, restoreStoredBundle, snapshotBackup,
} from '../api';
import type { BundlesResp, HelpResp, InspectResp, RestoreResp } from '../types';
import HelpIcon from './HelpIcon';

interface Props { onBack: () => void }

type Mode = 'merge' | 'replace';

/** A pending restore: either an uploaded file or a stored snapshot name. */
type Pending =
  | { kind: 'file'; file: File; label: string }
  | { kind: 'stored'; name: string; label: string };

function kb(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} kB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function BackupPage({ onBack }: Props) {
  const [data, setData] = useState<BundlesResp | null>(null);
  const [includeScan, setIncludeScan] = useState(false);
  const [mode, setMode] = useState<Mode>('merge');
  const [pending, setPending] = useState<Pending | null>(null);
  const [inspection, setInspection] = useState<InspectResp | null>(null);
  const [result, setResult] = useState<RestoreResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [help, setHelp] = useState<HelpResp | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    getHelp().then(setHelp).catch(() => { /* help is additive */ });
  }, []);

  const load = useCallback(async () => {
    try { setData(await getBundles()); setErr(null); }
    catch (e) { setErr((e as Error).message); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const takeSnapshot = useCallback(async () => {
    setBusy(true); setMsg(null); setErr(null);
    try {
      const r = await snapshotBackup(includeScan);
      setMsg(`Saved ${r.bundle} (${kb(r.bytes)})`
             + (r.pruned.length ? ` · pruned ${r.pruned.length} older snapshot(s)` : ''));
      await load();
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }, [includeScan, load]);

  const removeBundle = useCallback(async (name: string) => {
    if (!window.confirm(`Delete ${name}?`)) return;
    try { await deleteBundle(name); await load(); }
    catch (e) { setErr((e as Error).message); }
  }, [load]);

  const inspect = useCallback(async (p: Pending) => {
    setBusy(true); setErr(null); setMsg(null); setResult(null);
    setPending(p); setInspection(null);
    try {
      setInspection(p.kind === 'file'
        ? await inspectBundleFile(p.file)
        : await inspectStoredBundle(p.name));
    } catch (e) {
      setErr((e as Error).message);
      setPending(null);
    } finally { setBusy(false); }
  }, []);

  const restore = useCallback(async () => {
    if (!pending) return;
    const what = mode === 'replace'
      ? 'This overwrites the current configuration AND replaces every artwork '
        + 'override/lock with the ones in the bundle. Continue?'
      : 'This overwrites the current configuration. Artwork overrides are merged '
        + '(nothing you made after the backup is deleted). Continue?';
    if (!window.confirm(what)) return;
    setBusy(true); setErr(null); setMsg(null);
    try {
      const r = pending.kind === 'file'
        ? await restoreBundleFile(pending.file, mode)
        : await restoreStoredBundle(pending.name, mode);
      setResult(r);
      setPending(null); setInspection(null);
      if (fileInput.current) fileInput.current.value = '';
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }, [pending, mode]);

  const helpFor = (key: string) => help?.help[key]?.long;
  const diff = inspection?.diff;

  return (
    <div className="settings-page">
      <header className="settings-header">
        <button className="btn settings-back" onClick={onBack}>← Back</button>
        <h2>Backup &amp; Restore <HelpIcon help={helpFor('feature.page.backup')} /></h2>
      </header>

      <div className="settings-body">
        {err && <div className="field-msg err health-msg">{err}</div>}
        {msg && <div className="field-msg ok health-msg">{msg}</div>}

        <section className="backup-card">
          <h3>Back up</h3>
          <p className="field-hint muted">
            A bundle holds your configuration, library profiles, schedules and the artwork
            decisions that live outside config.yml — which candidate you picked, which image
            is locked, which kinds you skipped.
          </p>
          {data && (
            <p className="field-hint muted">
              <strong>Never included:</strong> {data.excluded.join(' · ')}. API keys are
              referenced by name only, so a restored install needs them re-entered — the
              bundle lists which.
            </p>
          )}
          <label className="health-field">
            <input type="checkbox" checked={includeScan}
                   onChange={(e) => setIncludeScan(e.target.checked)} />
            <span className="muted">Include the latest health-scan findings</span>
          </label>
          <div className="backup-actions">
            <a className="btn primary" href={backupExportUrl(includeScan)} download>
              Download backup
            </a>
            <button className="btn" disabled={busy} onClick={takeSnapshot}>
              Save a snapshot on the server
            </button>
          </div>
        </section>

        <section className="backup-card">
          <h3>
            Snapshots on this server
            {data && <span className="muted"> · keeping the newest {data.keep_bundles || '∞'}</span>}
          </h3>
          {data && data.bundles.length === 0 && (
            <p className="field-hint muted">No snapshots yet.</p>
          )}
          {data && data.bundles.length > 0 && (
            <table className="meta-table">
              <thead><tr><th>Name</th><th>Size</th><th>Saved</th><th></th></tr></thead>
              <tbody>
                {data.bundles.map((b) => (
                  <tr key={b.name}>
                    <td className="backup-name">{b.name}</td>
                    <td className="muted">{kb(b.bytes)}</td>
                    <td className="muted">
                      {new Date(b.modified_at * 1000).toLocaleString()}
                    </td>
                    <td className="activity-actions">
                      <a className="btn btn-ghost"
                         href={`/api/backup/bundles/${encodeURIComponent(b.name)}`}
                         download>Download</a>
                      <button className="btn" disabled={busy}
                              onClick={() => inspect({ kind: 'stored', name: b.name,
                                                       label: b.name })}>
                        Inspect…
                      </button>
                      <button className="btn danger" disabled={busy}
                              onClick={() => removeBundle(b.name)}>Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="backup-card">
          <h3>Restore</h3>
          <p className="field-hint muted">
            Inspecting writes nothing — it reads the bundle and reports what a restore would
            change. The restore itself goes through the same validation as the Settings
            editor, so a bundle that does not validate is refused before anything is touched,
            and the previous config stays available under Settings → Undo.
          </p>
          <div className="backup-actions">
            <input
              ref={fileInput} type="file" accept=".zip,application/zip" disabled={busy}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void inspect({ kind: 'file', file: f, label: f.name });
              }}
            />
            <label className="health-field">
              <span className="muted">Artwork overrides</span>
              <select value={mode} disabled={busy}
                      onChange={(e) => setMode(e.target.value as Mode)}>
                <option value="merge">merge (keep newer ones)</option>
                <option value="replace">replace (bundle wins)</option>
              </select>
            </label>
          </div>

          {inspection && pending && (
            <div className="backup-inspect">
              <h4>{pending.label}</h4>
              <p className="muted">
                Made {inspection.manifest.created_at} by Finmeta{' '}
                {inspection.manifest.finmeta_version || '?'} · members:{' '}
                {inspection.members.join(', ')}
              </p>
              <ul className="backup-diff">
                {diff?.libraries && (
                  <li>
                    <strong>Libraries:</strong>{' '}
                    {diff.libraries.added.length ? `+${diff.libraries.added.join(', ')} ` : ''}
                    {diff.libraries.removed.length ? `−${diff.libraries.removed.join(', ')} ` : ''}
                    {diff.libraries.kept.length ? `(unchanged: ${diff.libraries.kept.join(', ')})` : ''}
                    {!diff.libraries.added.length && !diff.libraries.removed.length
                      && !diff.libraries.kept.length ? 'none' : ''}
                  </li>
                )}
                {diff?.overlay_profiles && (
                  <li>
                    <strong>Overlay profiles:</strong>{' '}
                    {diff.overlay_profiles.added.length
                      ? `+${diff.overlay_profiles.added.join(', ')} ` : ''}
                    {diff.overlay_profiles.removed.length
                      ? `−${diff.overlay_profiles.removed.join(', ')}` : ''}
                    {!diff.overlay_profiles.added.length
                      && !diff.overlay_profiles.removed.length ? 'no change' : ''}
                  </li>
                )}
                {diff?.schedules && (
                  <li>
                    <strong>Schedules:</strong> {diff.schedules.current} now →{' '}
                    {diff.schedules.incoming} after
                  </li>
                )}
                {diff?.dry_run && (
                  <li>
                    <strong>dry_run:</strong> {String(diff.dry_run.current)} →{' '}
                    {String(diff.dry_run.incoming)}
                  </li>
                )}
                <li>
                  <strong>Artwork decisions in the bundle:</strong>{' '}
                  {Object.entries(inspection.overrides)
                    .map(([k, n]) => `${k} ${n}`).join(' · ') || 'none'}
                </li>
                {inspection.secrets_required.length > 0 && (
                  <li>
                    <strong>Keys to re-enter afterwards:</strong>{' '}
                    {inspection.secrets_required.join(', ')}
                  </li>
                )}
              </ul>
              <div className="backup-actions">
                <button className="btn danger" disabled={busy} onClick={restore}>
                  Restore this bundle ({mode})
                </button>
                <button className="btn btn-ghost" disabled={busy}
                        onClick={() => { setPending(null); setInspection(null); }}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {result && (
            <div className="backup-result field-msg ok">
              <div>
                Restored: config {result.restored.config ? 'yes' : 'no'} ·{' '}
                {Object.entries(result.restored.overrides)
                  .map(([k, n]) => `${k} ${n}`).join(' · ') || 'no override rows'}{' '}
                ({result.mode})
              </div>
              {result.secrets_required.length > 0 && (
                <div>Re-enter these keys in Settings: {result.secrets_required.join(', ')}</div>
              )}
              {Object.values(result.packs_to_reimport ?? {}).flat().length > 0 && (
                <div>
                  Re-import these packs:{' '}
                  {Object.entries(result.packs_to_reimport)
                    .map(([k, v]) => `${k}: ${(v || []).join(', ')}`).join(' · ')}
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

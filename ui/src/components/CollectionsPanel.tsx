// S9f2: built-in preset collections (seasonal genre + chart), toggled on/off.
// Same getConfig/putConfig round-trip as TrendingPanel/SchedulesPanel so
// server-side validation applies; persists immediately on toggle (no Save
// step), same UX as the Overlays-tab pack Activate/Deactivate buttons.
import { useCallback, useEffect, useRef, useState } from 'react';
import { getCollectionPresets, getConfig, putConfig, startRun, getJob,
         type CollectionPresetInfo } from '../api';
import type { JobResp } from '../types';
import * as YAML from 'js-yaml';

const TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);

export default function CollectionsPanel() {
  const [presets, setPresets] = useState<CollectionPresetInfo[]>([]);
  const [active, setActive] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Presets/seasonal collections only ever get built on a full (all-
  // libraries) plan/apply -- see engine.plan_builder._build_api_actions.
  // Nothing else in the app currently triggers that scope (per-library
  // apply, per-item apply, and even "all libraries" schedules all fan out
  // into one job PER library), so without this button the feature above is
  // unreachable regardless of which presets are ticked on.
  const [runJob, setRunJob] = useState<JobResp | null>(null);
  const [runAction, setRunAction] = useState<'plan' | 'apply' | null>(null);
  const [running, setRunning] = useState(false);
  const [runErr, setRunErr] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  function pollJob(jobId: string) {
    timer.current = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setRunJob(j);
        if (TERMINAL.has(j.status)) {
          if (timer.current) window.clearInterval(timer.current);
          setRunning(false);
        }
      } catch (e) {
        setRunErr((e as Error).message);
        if (timer.current) window.clearInterval(timer.current);
        setRunning(false);
      }
    }, 1500);
  }

  async function runAll(kind: 'plan' | 'apply') {
    setRunning(true); setRunErr(null); setRunAction(kind); setRunJob(null);
    try {
      const r = await startRun(kind); // no item/library -> full all-libraries scope
      pollJob(r.job_id);
    } catch (e) {
      setRunErr((e as Error).message);
      setRunning(false);
    }
  }

  const load = useCallback(async () => {
    try {
      const [catalog, c] = await Promise.all([getCollectionPresets(), getConfig()]);
      setPresets(catalog.presets);
      setActive((c.parsed?.collection_presets as string[] | undefined) ?? []);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function flash(m: string) { setMsg(m); setTimeout(() => setMsg(null), 3500); }

  async function toggle(id: string) {
    const next = active.includes(id) ? active.filter((x) => x !== id) : [...active, id];
    setBusy(true); setErr(null);
    try {
      const d = await getConfig();
      let doc: any;
      try { doc = (YAML.load(d.yaml) as any) ?? {}; } catch { doc = {}; }
      doc.collection_presets = next;
      const text = YAML.dump(doc, { noRefs: true, lineWidth: -1 });
      await putConfig(text);
      setActive(next);
      flash('Saved');
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const seasonal = presets.filter((p) => p.kind === 'seasonal_adaptive');
  const charts = presets.filter((p) => p.kind === 'chart');

  function Row({ p }: { p: CollectionPresetInfo }) {
    return (
      <label className="collection-preset-row">
        <input
          type="checkbox" disabled={busy} checked={active.includes(p.id)}
          onChange={() => void toggle(p.id)}
        />
        <div>
          <div className="collection-preset-label">{p.label}</div>
          <div className="field-hint muted">{p.description}</div>
        </div>
      </label>
    );
  }

  return (
    <div className="collections-panel">
      <h3>Collections</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Built-in preset collections, created and kept in sync in Jellyfin via the Jellyfin API
        (no collection.nfo). Tick any number on at once.
      </p>

      <h4>Seasonal</h4>
      <p className="field-hint muted">
        One toggle drives a whole calendar of holidays — each becomes its own Jellyfin collection,
        only during its own window, removed automatically outside it and recreated next time.
      </p>
      <div className="collection-preset-list">
        {seasonal.map((p) => <Row key={p.id} p={p} />)}
      </div>

      <h4>Charts</h4>
      <p className="field-hint muted">Always kept in sync.</p>
      <div className="collection-preset-list">
        {charts.map((p) => <Row key={p.id} p={p} />)}
      </div>

      {presets.length === 0 && !err && (
        <p className="field-hint muted">Loading preset catalog…</p>
      )}
      {msg && <div className="field-msg ok">{msg}</div>}
      {err && <div className="field-msg err">{err}</div>}

      <h4>Run now</h4>
      <p className="field-hint muted">
        The toggles above only take effect on a full, all-libraries plan/apply — not a
        single-library or single-item one. Use this to run one now instead of waiting for a
        schedule.
      </p>
      <div className="run-controls">
        <button className="btn" disabled={running} onClick={() => void runAll('plan')}>
          {running && runAction === 'plan' ? 'Planning…' : 'Plan (all libraries)'}
        </button>
        <button className="btn btn-primary" disabled={running} onClick={() => void runAll('apply')}>
          {running && runAction === 'apply' ? 'Applying…' : 'Apply (all libraries)'}
        </button>
      </div>
      {runErr && <div className="field-msg err">{runErr}</div>}
      {runJob && (
        <p className="field-hint muted">
          {runJob.status}
          {TERMINAL.has(runJob.status) &&
            ` — files written: ${runJob.files_written}, unchanged: ${runJob.files_unchanged}, api calls: ${runJob.api_calls}`}
        </p>
      )}
    </div>
  );
}

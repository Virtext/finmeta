// S12d: library statistics from a health scan. Four charts as plain CSS
// bars (no chart dependency): resolution, HDR / Dolby Vision, audio, and one
// tabbed chart for genre / decade / age rating. All libraries combined by
// default, or one library via the selector. Labels for the first three are
// the overlay badge labels (backend uses the same K9 format rules).
import { useEffect, useMemo, useState } from 'react';
import { getScanStats, type ScanStats } from '../api';

type Breakdown = Record<string, number>;

const META_TABS = [
  { key: 'genre', label: 'Genre' },
  { key: 'decade', label: 'Decade' },
  { key: 'official_rating', label: 'Age rating' },
] as const;

function merge(libs: ScanStats['libraries'], only: string | null): Record<string, Breakdown> {
  const out: Record<string, Breakdown> = {};
  for (const [lib, stats] of Object.entries(libs)) {
    if (only && lib !== only) continue;
    for (const [key, bucket] of Object.entries(stats)) {
      const o = (out[key] ??= {});
      for (const [label, n] of Object.entries(bucket)) o[label] = (o[label] ?? 0) + n;
    }
  }
  return out;
}

function Bars({ data, limit = 12, sortByLabel = false }: { data?: Breakdown; limit?: number; sortByLabel?: boolean }) {
  const rows = Object.entries(data ?? {});
  if (rows.length === 0) return <p className="field-hint muted">No data in this scan.</p>;
  rows.sort(sortByLabel ? (a, b) => a[0].localeCompare(b[0]) : (a, b) => b[1] - a[1]);
  const shown = rows.slice(0, limit);
  const rest = rows.slice(limit).reduce((s, [, n]) => s + n, 0);
  if (rest) shown.push([`${rows.length - limit} more`, rest]);
  const total = rows.reduce((s, [, n]) => s + n, 0);
  const max = Math.max(...shown.map(([, n]) => n));
  return (
    <div className="stats-bars">
      {shown.map(([label, n]) => (
        <div className="stats-bar-row" key={label}>
          <span className="stats-bar-label" title={label}>{label}</span>
          <span className="stats-bar-track">
            <span className="stats-bar-fill" style={{ width: `${(100 * n) / max}%` }} />
          </span>
          <span className="stats-bar-n">{n} <span className="muted">({Math.round((100 * n) / total)}%)</span></span>
        </div>
      ))}
    </div>
  );
}

export default function StatsPanel({ scanId, scanStatus }: { scanId: string; scanStatus: string }) {
  const [libs, setLibs] = useState<ScanStats['libraries'] | null>(null);
  const [only, setOnly] = useState<string | null>(null);
  const [metaTab, setMetaTab] = useState<(typeof META_TABS)[number]['key']>('genre');

  useEffect(() => {
    getScanStats(scanId).then((r) => setLibs(r.libraries)).catch(() => setLibs({}));
  }, [scanId, scanStatus]);

  const merged = useMemo(() => merge(libs ?? {}, only), [libs, only]);
  if (!libs) return null;
  const names = Object.keys(libs);
  if (names.length === 0) {
    return (
      <div className="stats-panel">
        <h3>Statistics</h3>
        <p className="field-hint muted">This scan has no statistics yet — run a new scan to collect them.</p>
      </div>
    );
  }
  const type = merged.type ?? {};

  return (
    <div className="stats-panel">
      <div className="stats-head">
        <h3>Statistics</h3>
        <span className="field-hint muted">
          {Object.entries(type).map(([k, n]) => `${n} ${k.toLowerCase()}${n === 1 ? '' : 's'}`).join(' · ')}
        </span>
        {names.length > 1 && (
          <select className="health-lib-filter" value={only ?? ''} onChange={(e) => setOnly(e.target.value || null)}>
            <option value="">All libraries</option>
            {names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        )}
      </div>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Resolution, HDR and audio cover movies only (shows have no streams of their own) and use the
        same rules as the overlay badges. They need the scan's "media" check switched on.
      </p>
      <div className="stats-grid">
        <section><h4>Resolution</h4><Bars data={merged.resolution} /></section>
        <section><h4>HDR / Dolby Vision</h4><Bars data={merged.dynamic_range} /></section>
        <section><h4>Audio</h4><Bars data={merged.audio} /></section>
        <section>
          <h4>
            {META_TABS.map((t) => (
              <button key={t.key} type="button"
                      className={`stats-tab ${metaTab === t.key ? 'active' : ''}`}
                      onClick={() => setMetaTab(t.key)}>
                {t.label}
              </button>
            ))}
          </h4>
          <Bars data={merged[metaTab]} sortByLabel={metaTab === 'decade'} limit={metaTab === 'decade' ? 20 : 12} />
        </section>
      </div>
    </div>
  );
}

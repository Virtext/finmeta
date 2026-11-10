import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  deleteScan, fixFindingsBatched, getConfig, getFindings, getHelp, getJob, getLatestScan, startScan,
} from '../api';
import type {
  FindingsResp, HelpResp, JobResp, LatestScanResp, LibraryView, ScanFinding, Severity,
} from '../types';
import HelpIcon from './HelpIcon';

interface Props {
  libraries: LibraryView[];
  dryRun: boolean;
  onBack: () => void;
  /** Open the artwork candidate picker for an item (main page, item scope). */
  onPickArtwork?: (itemId: string, itemName: string, library: string) => void;
}

const PAGE_SIZE = 100;
const TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);

// One card per check group, in the order a user cares about them.
const GROUP_LABELS: Record<string, string> = {
  artwork: 'Artwork',
  artwork_quality: 'Artwork quality',
  metadata: 'Metadata',
  media: 'Resolution / HDR',
  duplicates: 'Duplicates',
  episodes: 'Missing episodes',
  path_unmapped: 'Path mappings',
};
const GROUP_ORDER = Object.keys(GROUP_LABELS);

const SEVERITY_LABELS: Record<Severity, string> = {
  error: 'Errors',
  warning: 'Warnings',
  info: 'Info',
};

const CHECK_LABELS: Record<string, string> = {
  'artwork.missing': 'Missing artwork',
  'artwork.stale_jellyfin': 'Jellyfin has not picked up the file',
  'artwork.orphan_jellyfin': 'Image in Jellyfin, no file on disk',
  'artwork_quality.low_resolution': 'Low-resolution artwork',
  'artwork_quality.tiny_file': 'Suspiciously small artwork file',
  'artwork_quality.unreadable': 'Unreadable artwork file',
  'media.no_stream': 'No video stream',
  'media.missing_resolution': 'No resolution reported',
  'media.missing_hdr': 'No HDR information on a 4K item',
  'media.format_mismatch': 'Filename and stream disagree',
  'media.low_resolution': 'Below the resolution floor',
  'metadata.no_provider_id': 'Not identified',
  'metadata.missing_overview': 'No description',
  'metadata.missing_year': 'No year',
  'metadata.missing_fields': 'Thin metadata',
  'metadata.tmdb_unmatched': 'TMDb has no match',
  'metadata.year_mismatch': 'Probably the wrong title',
  'episodes.missing_season': 'Season missing entirely',
  'episodes.missing_episodes': 'Missing episodes',
  'episodes.not_in_sonarr': 'Not managed by Sonarr',
  'duplicates.same_provider_id': 'Same title twice',
  'duplicates.same_folder': 'Two movies in one folder',
  'duplicates.multi_version': 'Multiple versions in one item',
  path_unmapped: 'No path mapping',
};

/** Human label for a check code, falling back to the code itself. */
function checkLabel(code: string): string {
  if (CHECK_LABELS[code]) return CHECK_LABELS[code];
  const parts = code.split('.');
  // "artwork.missing.poster" -> "Missing artwork (poster)"
  const head = parts.slice(0, 2).join('.');
  if (CHECK_LABELS[head]) {
    return parts.length > 2 ? `${CHECK_LABELS[head]} (${parts[2]})` : CHECK_LABELS[head];
  }
  return code;
}

/** What this finding actually says, in a sentence, from its detail payload. */
function describe(f: ScanFinding): string {
  const d = f.detail || {};
  if (f.check_code.startsWith('artwork.missing.')) {
    return `No ${d.kind} on disk or in Jellyfin — Finmeta can download and write it.`;
  }
  if (f.check_code.startsWith('artwork.stale_jellyfin.')) {
    return `The ${d.kind} file exists but Jellyfin shows no ${d.kind}. It needs to re-read the folder.`;
  }
  if (f.check_code.startsWith('artwork.orphan_jellyfin.')) {
    return `Jellyfin has a ${d.kind}, but there is no ${d.kind} file Finmeta owns (so it cannot overlay it).`;
  }
  if (f.check_code === 'metadata.no_provider_id') {
    return 'No TMDb/IMDb/TVDb id, so Finmeta cannot look this item up at all — no artwork, no metadata, no overlays. Identify it in Jellyfin first.';
  }
  if (f.check_code === 'metadata.missing_overview') {
    return d.writes_nfo
      ? 'No description. Finmeta writes one into the NFO on apply (Jellyfin must be set to read NFO files).'
      : 'No description. This library does not write NFOs, so fix it in Jellyfin.';
  }
  if (f.check_code === 'metadata.missing_year') {
    return 'No production year, which also means the year cannot be checked against TMDb.';
  }
  if (f.check_code === 'metadata.missing_fields') {
    return `Empty: ${(d.fields || []).join(', ')}.`;
  }
  if (f.check_code === 'metadata.tmdb_unmatched') {
    return `TMDb has no match — ${d.reason || 'no reason recorded'}. Set the right provider id on the item in Jellyfin; Finmeta never guesses.`;
  }
  if (f.check_code === 'metadata.year_mismatch') {
    return `Jellyfin says ${d.jellyfin_year}, TMDb ${d.tmdb_id} says ${d.tmdb_year} (matched by ${d.matched_by}). A year this far out almost always means the wrong title.`;
  }
  if (f.check_code === 'episodes.missing_season') {
    return `Nothing from season ${d.season} is on disk — ${d.count} aired, monitored episode(s) missing.`;
  }
  if (f.check_code === 'episodes.missing_episodes') {
    return `Season ${d.season}: ${d.count} aired, monitored episode(s) have no file.`;
  }
  if (f.check_code === 'episodes.not_in_sonarr') {
    return 'No Sonarr series matches this show, so Finmeta cannot tell what is missing. Deliberately unmanaged shows are fine to ignore.';
  }
  if (f.check_code === 'duplicates.same_provider_id') {
    return `${d.count} items share ${d.provider} id ${d.provider_id}.`;
  }
  if (f.check_code === 'duplicates.same_folder') {
    return `${d.count} movies resolve to the same folder — they would fight over the same poster.jpg.`;
  }
  if (f.check_code === 'duplicates.multi_version') {
    return `${d.versions} versions in one item. ${d.note || ''}`.trim();
  }
  if (f.check_code.startsWith('artwork_quality.low_resolution.')) {
    return `${d.kind} is ${d.width}×${d.height}, below the ${d.floor}px floor. ${d.note || ''}`.trim();
  }
  if (f.check_code.startsWith('artwork_quality.tiny_file.')) {
    return `The ${d.kind} file is only ${Math.round((d.bytes || 0) / 1024)} kB (floor ${Math.round((d.floor_bytes || 0) / 1024)} kB) — almost certainly a placeholder or a truncated download.`;
  }
  if (f.check_code.startsWith('artwork_quality.unreadable.')) {
    return `No image library can open this ${d.kind} file, so neither Jellyfin nor the overlay renderer can use it.`;
  }
  if (f.check_code === 'media.no_stream') {
    return 'Jellyfin reports no video stream — it has never probed this file, or the file is unreadable.';
  }
  if (f.check_code === 'media.missing_resolution') {
    return 'The video stream has no height, so the resolution badge has nothing to show.';
  }
  if (f.check_code === 'media.missing_hdr') {
    return `4K (${d.height}px) with no dynamic-range information at all — not even SDR — so the HDR badge silently disappears.`;
  }
  if (f.check_code === 'media.format_mismatch') {
    const what = d.aspect === 'resolution' ? 'resolution' : 'dynamic range';
    return `The filename claims ${d.claimed} (${what}) but the stream reports ${d.detected}.`;
  }
  if (f.check_code === 'media.low_resolution') {
    return `Video is ${d.height}px tall, below the ${d.floor}px floor you configured.`;
  }
  if (f.check_code === 'path_unmapped') {
    return `${d.count} item(s) under ${d.prefix} have no path mapping, so every disk check is blind for them.`;
  }
  return JSON.stringify(d);
}

/** Extra rows worth showing under the sentence (paths, members, samples). */
function detailLines(f: ScanFinding): string[] {
  const d = f.detail || {};
  if (Array.isArray(d.missing)) {
    const lines = [d.missing.join(', ')];
    if (d.missing_truncated) lines.push(`…and ${d.missing_truncated} more`);
    return lines;
  }
  if (Array.isArray(d.members)) {
    const lines = d.members.map((m: any) => `${m.name} — ${m.path || '(no path)'}`);
    if (d.members_truncated) lines.push(`…and ${d.members_truncated} more`);
    return lines;
  }
  if (Array.isArray(d.samples)) return d.samples;
  if (typeof d.file === 'string') return [d.file];
  if (typeof d.path === 'string' && d.path) return [d.path];
  return [];
}

const FIX_LABELS: Record<string, string> = {
  apply_item: 'Fix (apply)',
  refresh_item: 'Refresh in Jellyfin',
  // 2026-11-10: "try to upgrade low quality artwork" -- best-effort
  // reshuffle+apply via the same endpoint, alongside the still-offered
  // manual "Pick artwork…" link (onPickArtwork) for when it doesn't help.
  candidate_picker: 'Try to upgrade',
  external: '',
  none: '',
};

export default function HealthPage({ libraries, dryRun, onBack, onPickArtwork }: Props) {
  const [scope, setScope] = useState<string>('');            // '' = all libraries
  const [deep, setDeep] = useState(false);                   // tier 2
  const [latest, setLatest] = useState<LatestScanResp | null>(null);
  const [findings, setFindings] = useState<FindingsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [groupFilter, setGroupFilter] = useState<string | null>(null);
  const [severityFilter, setSeverityFilter] = useState<Severity | null>(null);
  const [libFilter, setLibFilter] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const [job, setJob] = useState<JobResp | null>(null);
  const [scanning, setScanning] = useState(false);
  const timer = useRef<number | null>(null);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [fixMsg, setFixMsg] = useState<string | null>(null);
  const [fixErr, setFixErr] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [jellyfinUrl, setJellyfinUrl] = useState<string>('');
  const [help, setHelp] = useState<HelpResp | null>(null);
  // Help text lives in the backend registry (SPEC S9 §B), never here.
  const helpFor = useCallback(
    (key: string) => help?.help[key]?.long, [help]);

  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  useEffect(() => {
    getHelp().then(setHelp).catch(() => { /* help is additive */ });
  }, []);

  useEffect(() => {
    getConfig()
      .then((c) => setJellyfinUrl(String(c.parsed?.server?.url || '').replace(/\/+$/, '')))
      .catch(() => { /* deep links are a nice-to-have */ });
  }, []);

  const loadFindings = useCallback(async (scanId: string, nextOffset = 0) => {
    const resp = await getFindings(scanId, {
      check: groupFilter || undefined,
      severity: severityFilter || undefined,
      library: libFilter || undefined,
      limit: PAGE_SIZE,
      offset: nextOffset,
    });
    setFindings((prev) => (nextOffset > 0 && prev
      ? { ...resp, findings: [...prev.findings, ...resp.findings] }
      : resp));
    setOffset(nextOffset);
  }, [groupFilter, severityFilter, libFilter]);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const l = await getLatestScan();
      setLatest(l);
      if (l.scan) await loadFindings(l.scan.scan_id, 0);
      else setFindings(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [loadFindings]);

  useEffect(() => { reload(); }, [reload]);

  const runScan = useCallback(() => {
    setScanning(true); setFixMsg(null); setFixErr(null); setError(null); setJob(null);
    startScan(scope || undefined, deep ? 2 : 1).then((r) => {
      timer.current = window.setInterval(async () => {
        try {
          const jb = await getJob(r.job_id);
          setJob(jb);
          if (TERMINAL.has(jb.status)) {
            if (timer.current) window.clearInterval(timer.current);
            setScanning(false);
            setSelected(new Set());
            reload();
          }
        } catch (e) {
          if (timer.current) window.clearInterval(timer.current);
          setError((e as Error).message);
          setScanning(false);
        }
      }, 1500);
    }).catch((e) => { setError((e as Error).message); setScanning(false); });
  }, [scope, deep, reload]);

  const progressLine = useMemo(() => {
    const lines = (job?.log || []).filter((l) => l.message.includes('progress'));
    return lines.length ? lines[lines.length - 1].message : null;
  }, [job]);

  const grouped = useMemo(() => {
    const out = new Map<string, ScanFinding[]>();
    for (const f of findings?.findings || []) {
      const key = f.check_code;
      if (!out.has(key)) out.set(key, []);
      out.get(key)!.push(f);
    }
    return [...out.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [findings]);

  const fixable = useCallback(
    (f: ScanFinding) => f.fix_kind === 'apply_item' || f.fix_kind === 'refresh_item'
      || f.fix_kind === 'candidate_picker',
    [],
  );

  const toggle = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const toggleGroup = (code: string) => setCollapsed((prev) => {
    const next = new Set(prev);
    if (next.has(code)) next.delete(code); else next.add(code);
    return next;
  });

  const submitFix = useCallback(async (targets: ScanFinding[]) => {
    if (!latest?.scan || !targets.length) return;
    setFixMsg(null); setFixErr(null);
    try {
      // Batched: the backend refuses more than 50 distinct items in one
      // request, and "fix selected" across a whole library's findings
      // routinely exceeds that (operator hit "153 > 50" on a real
      // deployment, same endpoint this page also calls).
      const r = await fixFindingsBatched(latest.scan.scan_id, targets);
      const started = r.submitted.filter((s) => s.run_id).length;
      const queued = r.submitted.filter((s) => s.queued).length;
      const refreshed = r.submitted.filter((s) => s.action === 'refresh_item' && !s.error).length;
      const failed = r.submitted.filter((s) => s.error).length;
      const parts: string[] = [];
      if (started) parts.push(`${started} apply run started`);
      if (queued) parts.push(`${queued} queued`);
      if (refreshed) parts.push(`${refreshed} Jellyfin refresh requested`);
      if (failed) parts.push(`${failed} failed`);
      if (r.not_fixable.length) parts.push(`${r.not_fixable.length} not fixable here`);
      setFixMsg(`${parts.join(' · ') || 'Nothing to do'}. Rescan to confirm.`);
      setSelected(new Set());
      const l = await getLatestScan();
      setLatest(l);
      if (l.scan) await loadFindings(l.scan.scan_id, 0);
    } catch (e) {
      setFixErr((e as Error).message);
    }
  }, [latest, loadFindings]);

  const clearScan = useCallback(async () => {
    if (!latest?.scan) return;
    if (!window.confirm('Delete this scan and its findings?')) return;
    try {
      await deleteScan(latest.scan.scan_id);
      setFindings(null);
      await reload();
    } catch (e) {
      setFixErr((e as Error).message);
    }
  }, [latest, reload]);

  const counts = latest?.counts;
  const scan = latest?.scan || null;
  const selectedFixable = useMemo(
    () => (findings?.findings || []).filter((f) => selected.has(f.id) && fixable(f)),
    [findings, selected, fixable],
  );

  return (
    <div className="settings-page">
      <header className="settings-header">
        <button className="btn settings-back" onClick={onBack}>← Back</button>
        <h2>Library health <HelpIcon help={helpFor('feature.page.health')} /></h2>
        {scan && (
          <button className="btn btn-ghost" onClick={clearScan} title="Delete this scan">
            Clear results
          </button>
        )}
      </header>

      <div className="health-bar">
        <label className="health-field">
          <span className="muted">Scope</span>
          <select value={scope} onChange={(e) => setScope(e.target.value)} disabled={scanning}>
            <option value="">All configured libraries</option>
            {libraries.map((l) => (
              <option key={l.name} value={l.name}>{l.name}</option>
            ))}
          </select>
        </label>
        <label
          className="health-field"
          title="Adds the TMDb match checks and Sonarr's missing-episode list. Much slower: one TMDb lookup per item and one Sonarr request per series."
        >
          <input
            type="checkbox" checked={deep} disabled={scanning}
            onChange={(e) => setDeep(e.target.checked)}
          />
          <span className="muted">Deep scan (TMDb / Sonarr)</span>
        </label>
        <button className="btn primary" onClick={runScan} disabled={scanning}>
          {scanning ? 'Scanning…' : 'Scan now'}
        </button>
        <span className="muted health-note">
          A scan is read-only and works under DRY RUN, but it holds the single run slot
          while it works — applies wait for it.
        </span>
      </div>

      {scanning && (
        <div className="health-progress">
          <span className="spinner-dot" /> {progressLine || 'Starting…'}
        </div>
      )}
      {error && <div className="field-msg err health-msg">{error}</div>}

      <div className="settings-body">
        {loading && <p className="muted">Loading…</p>}

        {!loading && !scan && (
          <div className="health-empty">
            <h3>No scan yet</h3>
            <p className="muted">
              A scan walks the configured libraries and reports what is missing or wrong:
              artwork Finmeta can write, posters Jellyfin has not picked up, duplicate
              titles, and libraries with no path mapping. Nothing is written until you
              click a fix.
            </p>
          </div>
        )}

        {scan && (
          <>
            <div className="health-summary">
              <div className="health-scan-meta">
                <strong>{scan.finding_count}</strong> finding(s) in{' '}
                <strong>{scan.items_scanned}</strong> item(s) ·{' '}
                {scan.library || 'all libraries'} ·{' '}
                {scan.tier >= 2 ? 'deep scan' : 'quick scan'} ·{' '}
                {scan.status === 'running' ? 'running…' : scan.status} ·{' '}
                {new Date(scan.started_at).toLocaleString()}
                {scan.status === 'error' && (
                  <span className="field-msg err"> partial results — see the run log</span>
                )}
              </div>
              <div className="health-chips">
                {(['error', 'warning', 'info'] as Severity[]).map((s) => (
                  <button
                    key={s}
                    className={`health-chip sev-${s} ${severityFilter === s ? 'active' : ''}`}
                    onClick={() => { setSeverityFilter(severityFilter === s ? null : s); }}
                  >
                    {SEVERITY_LABELS[s]} <strong>{counts?.by_severity?.[s] || 0}</strong>
                  </button>
                ))}
                {libraries.length > 1 && (
                  <select
                    className="health-lib-filter"
                    value={libFilter || ''}
                    onChange={(e) => setLibFilter(e.target.value || null)}
                  >
                    <option value="">Every library</option>
                    {libraries.map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
                  </select>
                )}
              </div>
              <div className="health-cards">
                {GROUP_ORDER.filter((g) => (counts?.by_group?.[g] || 0) > 0).map((g) => (
                  <button
                    key={g}
                    className={`health-card ${groupFilter === g ? 'active' : ''}`}
                    onClick={() => setGroupFilter(groupFilter === g ? null : g)}
                  >
                    <span className="health-card-n">{counts?.by_group?.[g]}</span>
                    <span className="health-card-label">{GROUP_LABELS[g]}</span>
                  </button>
                ))}
                {!scan.finding_count && (
                  <div className="health-clean">Nothing to fix 🎉</div>
                )}
              </div>
            </div>

            {(groupFilter || severityFilter || libFilter) && (
              <div className="health-filters muted">
                Filtered: {[groupFilter && GROUP_LABELS[groupFilter], severityFilter,
                  libFilter].filter(Boolean).join(' · ')}
                <button
                  className="btn btn-ghost"
                  onClick={() => { setGroupFilter(null); setSeverityFilter(null); setLibFilter(null); }}
                >
                  Clear filters
                </button>
              </div>
            )}

            {selectedFixable.length > 0 && (
              <div className="health-bulk">
                <span>{selectedFixable.length} selected</span>
                <button
                  className="btn primary"
                  onClick={() => submitFix(selectedFixable)}
                >
                  Fix selected
                </button>
                <button className="btn btn-ghost" onClick={() => setSelected(new Set())}>
                  Deselect
                </button>
              </div>
            )}
            {fixMsg && <div className="field-msg ok health-msg">{fixMsg}</div>}
            {fixErr && <div className="field-msg err health-msg">{fixErr}</div>}

            {grouped.map(([code, rows]) => (
              <section className="health-group" key={code}>
                <h3 onClick={() => toggleGroup(code)}>
                  <span className="health-group-caret">
                    {collapsed.has(code) ? '▸' : '▾'}
                  </span>
                  {checkLabel(code)}
                  <HelpIcon help={helpFor(`feature.scan.${rows[0].group}`)} />
                  <span className="muted health-group-count">{rows.length}</span>
                  <span className={`health-sev sev-${rows[0].severity}`}>
                    {rows[0].severity}
                  </span>
                </h3>
                {!collapsed.has(code) && (
                  <ul className="health-rows">
                    {rows.map((f) => (
                      <li className="health-row" key={f.id}>
                        {fixable(f) && (
                          <input
                            type="checkbox"
                            checked={selected.has(f.id)}
                            onChange={() => toggle(f.id)}
                            aria-label={`Select ${f.item_name}`}
                          />
                        )}
                        <div className="health-row-main">
                          <div className="health-row-head">
                            <strong>{f.item_name || f.library}</strong>
                            <span className="muted">{f.library}</span>
                            {f.fixed_at && (
                              <span className="badge skipped">fix submitted — rescan to confirm</span>
                            )}
                          </div>
                          <div className="health-row-desc">{describe(f)}</div>
                          {detailLines(f).map((line, i) => (
                            <div className="health-row-path muted" key={i}>{line}</div>
                          ))}
                        </div>
                        <div className="health-row-actions">
                          {fixable(f) && (
                            <button
                              className="btn"
                              disabled={(f.fix_kind === 'apply_item' || f.fix_kind === 'candidate_picker') && dryRun}
                              title={(f.fix_kind === 'apply_item' || f.fix_kind === 'candidate_picker') && dryRun
                                ? 'dry_run is true — set it false in config to write files'
                                : undefined}
                              onClick={() => submitFix([f])}
                            >
                              {FIX_LABELS[f.fix_kind]}
                            </button>
                          )}
                          {f.fix_kind === 'candidate_picker' && f.item_id && onPickArtwork && (
                            <button
                              className="btn"
                              onClick={() => onPickArtwork(f.item_id!, f.item_name, f.library)}
                            >
                              {FIX_LABELS.candidate_picker}
                            </button>
                          )}
                          {f.fix_kind === 'external' && f.detail?.sonarr && (
                            <a className="btn" href={String(f.detail.sonarr)}
                               target="_blank" rel="noreferrer">
                              Open in Sonarr
                            </a>
                          )}
                          {f.fix_kind === 'external' && !f.detail?.sonarr
                            && f.item_id && jellyfinUrl && (
                            <a
                              className="btn"
                              href={`${jellyfinUrl}/web/#/details?id=${encodeURIComponent(f.item_id)}`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              Open in Jellyfin
                            </a>
                          )}
                          {f.fix_kind === 'none' && (
                            <span className="muted health-nofix">report only</span>
                          )}
                          {f.check_code === 'path_unmapped' && (
                            <span className="muted health-nofix">fix in Settings</span>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ))}

            {findings?.has_more && (
              <button
                className="btn load-more-btn"
                onClick={() => scan && loadFindings(scan.scan_id, offset + PAGE_SIZE)}
              >
                Load more ({findings.findings.length} of {findings.total})
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

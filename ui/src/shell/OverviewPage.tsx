import React, { useCallback, useEffect, useState } from 'react';
import {
  Badge, Button, Card, CardHeader, CardBody, EmptyState, ErrorState,
  PageHeader, Skeleton, StatusDot, Table, useToast,
} from '../ui';
import type { BadgeTone } from '../ui';
import {
  fixFindings, getActivity, getFindings, getLatestScan, getLibraries, getServicesStatus, startScan,
} from '../api';
import type { ServiceStatusEntry } from '../api';
import type { ActivityResp, ActivityRun, LatestScanResp, LibraryView, ScanFinding } from '../types';
import { navigate } from './router';

// Real data only (no invented numbers, unlike the design mockup): this is
// the SPEC_S14_UI.md §5 claim that S13a "is" the Overview page, made true.
// Every number here comes straight from an existing endpoint.

function timeAgo(iso: string | null): string {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0 || Number.isNaN(ms)) return '—';
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function timeUntil(iso: string | null): string {
  if (!iso) return '—';
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return '—';
  if (ms <= 0) return 'due now';
  const m = Math.floor(ms / 60000);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  return `in ${h}h ${m % 60}m`;
}

function runLabel(run: { action: string | null; scope: string | null }): string {
  const action = run.action ?? 'run';
  if (!run.scope || run.scope === 'all') return `${action} · all libraries`;
  if (run.scope.startsWith('lib:')) return `${action} · ${run.scope.slice(4)}`;
  if (run.scope.startsWith('item:')) return `${action} · 1 item`;
  return action;
}

function runTone(status: string): BadgeTone {
  if (status === 'ok') return 'ok';
  if (status === 'error' || status === 'refused') return 'danger';
  if (status === 'partial') return 'warn';
  if (status === 'running') return 'accent';
  return 'neutral';
}

const GROUP_LABELS: Record<string, string> = {
  artwork: 'Missing artwork',
  media: 'Media / stream issues',
  duplicates: 'Duplicates',
  artwork_quality: 'Low-quality artwork',
  metadata: 'Metadata issues',
  episodes: 'Episode gaps',
  path_unmapped: 'Unmapped paths',
};

// Groups the Artwork page can one-click-fix (apply_item/refresh_item) --
// everything else (metadata, duplicates, episodes, path_unmapped, most of
// media) needs a human or an external tool, so "Needs attention" only
// offers a Fix action for these two.
const ARTWORK_GROUPS = ['artwork', 'artwork_quality'];

export function OverviewPage(): React.ReactElement {
  const { push } = useToast();
  const [libs, setLibs] = useState<LibraryView[] | null>(null);
  const [scan, setScan] = useState<LatestScanResp | null>(null);
  const [activity, setActivity] = useState<ActivityResp | null>(null);
  const [services, setServices] = useState<ServiceStatusEntry[] | null>(null);
  const [artworkFindings, setArtworkFindings] = useState<ScanFinding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [fixingAll, setFixingAll] = useState(false);

  const load = useCallback(() => {
    setError(null);
    Promise.all([getLibraries(), getLatestScan(), getActivity({ limit: 5 })])
      .then(([l, s, a]) => {
        setLibs(l.libraries); setScan(s); setActivity(a);
        // Artwork Coverage + the fixable part of "Needs attention" both need
        // the actual findings, not just their counts -- same two calls
        // ArtworkPage itself makes, capped the same way (200/group).
        if (s.scan) {
          Promise.all([
            getFindings(s.scan.scan_id, { check: 'artwork', limit: 200 }),
            getFindings(s.scan.scan_id, { check: 'artwork_quality', limit: 200 }),
          ]).then(([x, y]) => setArtworkFindings([...x.findings, ...y.findings]))
            .catch(() => setArtworkFindings([]));
        } else {
          setArtworkFindings([]);
        }
      })
      .catch((e: any) => setError(e?.message ?? 'Could not load the overview.'));
    // Separate request, separate failure mode: a slow/broken provider
    // check must never block the rest of the dashboard from rendering.
    getServicesStatus().then((r) => setServices(r.services)).catch(() => setServices([]));
  }, []);

  useEffect(() => { load(); }, [load]);

  const runScan = () => {
    setScanning(true);
    startScan(undefined, 1)
      .then(() => { push({ message: 'Scan started — check Activity for progress.' }); load(); })
      .catch((e: any) => push({ message: e?.message ?? 'Could not start the scan.', tone: 'danger' }))
      .finally(() => setScanning(false));
  };

  const fixAllArtwork = () => {
    const scanId = scan?.scan?.scan_id;
    const fixable = (artworkFindings ?? []).filter((f) => f.fix_kind === 'apply_item' || f.fix_kind === 'refresh_item');
    if (!scanId || fixable.length === 0) return;
    setFixingAll(true);
    fixFindings(scanId, fixable.map((f) => f.id))
      .then((r) => {
        const started = r.submitted.filter((s) => s.run_id || s.queued).length;
        push({ message: `${started} fix${started === 1 ? '' : 'es'} submitted. Rescan to confirm.` });
        load();
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not submit the fix.', tone: 'danger' }))
      .finally(() => setFixingAll(false));
  };

  if (error) {
    return (
      <ErrorState
        title="Could not load the overview"
        description={error}
        action={<Button onClick={load}>Retry</Button>}
      />
    );
  }

  const loading = libs === null || scan === null || activity === null;
  const severities = scan?.counts.by_severity ?? {};
  const groups = Object.entries(scan?.counts.by_group ?? {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const nextSchedule = activity?.schedules.find((s) => s.next_run_at)?.next_run_at ?? null;

  const itemsScanned = scan?.scan?.items_scanned ?? 0;
  const affectedItems = new Set((artworkFindings ?? []).map((f) => f.item_id).filter(Boolean)).size;
  const coveragePct = itemsScanned > 0 ? Math.max(0, Math.round(100 * (1 - affectedItems / itemsScanned))) : null;
  const fixableCount = (artworkFindings ?? []).filter((f) => f.fix_kind === 'apply_item' || f.fix_kind === 'refresh_item').length;
  const needsHumanCount = (artworkFindings ?? []).filter((f) => f.fix_kind === 'candidate_picker').length;

  return (
    <div>
      <PageHeader
        title="Overview"
        description={libs ? `${libs.length} librar${libs.length === 1 ? 'y' : 'ies'} managed` : undefined}
        actions={<Button variant="primary" loading={scanning} onClick={runScan}>Scan libraries</Button>}
      />

      <div className="fm-grid-3">
        <Card>
          <CardHeader title="Latest scan" />
          <CardBody>
            {loading ? (
              <Skeleton height={60} />
            ) : scan!.scan === null ? (
              <EmptyState icon="search" title="No scan yet" description="Run one to see artwork and metadata health here." />
            ) : (
              <>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                  {(severities.error ?? 0) > 0 && <Badge tone="danger">{severities.error} error{severities.error === 1 ? '' : 's'}</Badge>}
                  {(severities.warning ?? 0) > 0 && <Badge tone="warn">{severities.warning} warning{severities.warning === 1 ? '' : 's'}</Badge>}
                  {(severities.info ?? 0) > 0 && <Badge tone="neutral">{severities.info} info</Badge>}
                  {Object.keys(severities).length === 0 && <Badge tone="ok">No findings</Badge>}
                </div>
                {groups.length > 0 && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 'var(--text-sm)', color: 'var(--text-muted)' }}>
                    {/* Clicking a group jumps to where its specifics actually
                        live -- the group counts here only ever told the
                        operator numbers, never which items (operator
                        feedback: "can't see specifics from the main
                        screen"). */}
                    {groups.map(([g, n]) => (
                      <button
                        key={g}
                        className="fm-overview-grouprow"
                        onClick={() => navigate(ARTWORK_GROUPS.includes(g) ? '/artwork' : '/activity')}
                      >
                        <span>{GROUP_LABELS[g] ?? g}</span>
                        <span style={{ color: 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>{n}</span>
                      </button>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-subtle)', fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>
                  Scan #{scan!.scan!.scan_id.slice(0, 8)} · {timeAgo(scan!.scan!.finished_at ?? scan!.scan!.started_at)}
                  {scan!.scan!.library ? ` · ${scan!.scan!.library}` : ' · all libraries'}
                </div>
              </>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Runs" />
          <CardBody>
            {loading ? <Skeleton height={60} /> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {activity!.active && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)' }}>
                    <Badge tone="accent">now</Badge>
                    <span>{runLabel(activity!.active)}</span>
                  </div>
                )}
                {activity!.runs.slice(0, 4).map((r: ActivityRun) => (
                  <div key={r.run_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)' }}>
                    <StatusDot tone={runTone(r.status)} title={r.status} />
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{runLabel(r)}</span>
                    <span style={{ color: 'var(--text-faint)', fontSize: 'var(--text-xs)' }}>{timeAgo(r.finished_at)}</span>
                  </div>
                ))}
                {!activity!.active && activity!.runs.length === 0 && (
                  <EmptyState icon="clock" title="No runs yet" description="Plan or apply a library to see runs here." />
                )}
                {nextSchedule && (
                  <div style={{ marginTop: 4, paddingTop: 10, borderTop: '1px solid var(--border-subtle)', fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>
                    Next scheduled run {timeUntil(nextSchedule)}
                  </div>
                )}
              </div>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Libraries" />
          <CardBody>
            {loading ? <Skeleton height={60} /> : (
              <Table<LibraryView>
                columns={[
                  { key: 'name', header: 'Library', render: (l) => l.name },
                  { key: 'type', header: 'Type', render: (l) => <span style={{ color: 'var(--text-faint)' }}>{l.type}</span> },
                  {
                    key: 'overlays', header: 'Overlays',
                    render: (l) => (l.overlay_profile && l.overlay_element_count > 0
                      ? `${l.overlay_profile} (${l.overlay_element_count})`
                      : <span style={{ color: 'var(--text-faint)' }}>none</span>),
                  },
                ]}
                rows={libs!}
                rowKey={(l) => l.name}
              />
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Services" />
          <CardBody>
            {services === null ? (
              <Skeleton height={60} />
            ) : services.length === 0 ? (
              <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>Could not reach the service check.</span>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                {services.map((s) => (
                  <div key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)' }}>
                    <StatusDot tone={!s.enabled ? 'neutral' : s.ok ? 'accent' : 'danger'} title={!s.enabled ? 'off' : s.ok ? 'ok' : 'error'} />
                    <span style={{ flex: 1 }}>{s.label}</span>
                    <span style={{ color: 'var(--text-faint)', fontSize: 'var(--text-xs)' }}>
                      {!s.enabled ? 'off' : s.ok ? `${s.latency_ms ?? '?'} ms` : (s.error ?? 'error')}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Artwork coverage" />
          <CardBody>
            {loading || artworkFindings === null ? <Skeleton height={60} /> : scan!.scan === null ? (
              <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>Run a scan to see coverage.</span>
            ) : (
              <>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span style={{ fontSize: 'var(--text-2xl)', fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
                    {coveragePct === null ? '—' : `${coveragePct}%`}
                  </span>
                  <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>clean of {itemsScanned || '—'} scanned</span>
                </div>
                <div style={{ marginTop: 10, fontSize: 'var(--text-sm)', color: 'var(--text-muted)' }}>
                  {affectedItems > 0
                    ? `${affectedItems} item${affectedItems === 1 ? '' : 's'} with a poster/backdrop/logo problem`
                    : 'No artwork problems found'}
                </div>
                {affectedItems > 0 && (
                  <Button size="sm" variant="ghost" style={{ marginTop: 8 }} onClick={() => navigate('/artwork')}>View in Artwork →</Button>
                )}
              </>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Needs attention" />
          <CardBody>
            {loading || artworkFindings === null ? <Skeleton height={60} /> : (
              <>
                {groups.length === 0 ? (
                  <EmptyState icon="check" title="Nothing needs attention" description="The latest scan found no findings." />
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 'var(--text-sm)' }}>
                    {fixableCount > 0 && <div><Badge tone="warn">{fixableCount}</Badge> <span style={{ marginLeft: 6 }}>fixable automatically right now</span></div>}
                    {needsHumanCount > 0 && <div><Badge tone="neutral">{needsHumanCount}</Badge> <span style={{ marginLeft: 6 }}>need a manual artwork pick</span></div>}
                    {(severities.error ?? 0) > 0 && <div><Badge tone="danger">{severities.error}</Badge> <span style={{ marginLeft: 6 }}>error-severity findings overall</span></div>}
                  </div>
                )}
                <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                  {fixableCount > 0 && (
                    <Button size="sm" loading={fixingAll} onClick={fixAllArtwork}>Fix {fixableCount} automatically</Button>
                  )}
                  {groups.length > 0 && (
                    <Button size="sm" variant="ghost" onClick={() => navigate('/artwork')}>Open Artwork →</Button>
                  )}
                </div>
              </>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Quick actions" />
          <CardBody>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Button size="sm" variant="ghost" onClick={() => navigate('/library')}>Browse a library</Button>
              <Button size="sm" variant="ghost" onClick={() => navigate('/overlays')}>Overlay profiles</Button>
              <Button size="sm" variant="ghost" onClick={() => navigate('/packs')}>Manage packs</Button>
              <Button size="sm" variant="ghost" onClick={() => navigate('/automation')}>Schedules & automation</Button>
            </div>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

export default OverviewPage;

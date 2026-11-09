import React, { useCallback, useEffect, useState } from 'react';
import {
  Badge, Button, Card, CardHeader, CardBody, EmptyState, ErrorState,
  PageHeader, Skeleton, StatusDot, Table, useToast,
} from '../ui';
import type { BadgeTone } from '../ui';
import { getActivity, getLatestScan, getLibraries, getServicesStatus, startScan } from '../api';
import type { ServiceStatusEntry } from '../api';
import type { ActivityResp, ActivityRun, LatestScanResp, LibraryView } from '../types';

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

export function OverviewPage(): React.ReactElement {
  const { push } = useToast();
  const [libs, setLibs] = useState<LibraryView[] | null>(null);
  const [scan, setScan] = useState<LatestScanResp | null>(null);
  const [activity, setActivity] = useState<ActivityResp | null>(null);
  const [services, setServices] = useState<ServiceStatusEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  const load = useCallback(() => {
    setError(null);
    Promise.all([getLibraries(), getLatestScan(), getActivity({ limit: 5 })])
      .then(([l, s, a]) => { setLibs(l.libraries); setScan(s); setActivity(a); })
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
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 'var(--text-sm)', color: 'var(--text-muted)' }}>
                    {groups.map(([g, n]) => (
                      <div key={g} style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span>{GROUP_LABELS[g] ?? g}</span>
                        <span style={{ color: 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>{n}</span>
                      </div>
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
                  { key: 'overlays', header: 'Overlays', render: (l) => (l.overlays.length > 0 ? l.overlays.join(', ') : '—') },
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
      </div>
    </div>
  );
}

export default OverviewPage;

import React, { useCallback, useEffect, useState } from 'react';
import {
  Badge, Button, Card, Checkbox, EmptyState, ErrorState, PageHeader, Skeleton, useToast,
} from '../ui';
import type { BadgeTone } from '../ui';
import { fixFindings, getFindings, getLatestScan, startScan } from '../api';
import type { ScanFinding } from '../types';
import { navigate } from './router';

// SPEC_S14_UI.md phase P4: "Artwork page" does NOT re-build candidate
// compare/lock/apply -- shell/LibraryPage.tsx's item route already has all
// of that, reusing the real, pre-existing CandidatePicker/PreviewPane.
// What's actually missing, and what this page is, is a CROSS-LIBRARY
// triage list: which items have broken artwork right now (from the S10
// scan's "artwork" + "artwork_quality" groups), with a real one-click fix
// for the two kinds the scanner already knows how to fix automatically
// (apply_item, refresh_item -- via the SAME POST /api/scans/{id}/fix
// HealthPage.tsx already uses), and a "Pick artwork…" link into the real
// per-item view for the kind that genuinely needs a human choice
// (candidate_picker, e.g. low_resolution).

function describe(f: ScanFinding): string {
  const d = f.detail || {};
  if (f.check_code.startsWith('artwork.missing.')) return `No ${d.kind} on disk or in Jellyfin.`;
  if (f.check_code.startsWith('artwork.stale_jellyfin.')) return `${d.kind} file exists, but Jellyfin hasn't picked it up.`;
  if (f.check_code.startsWith('artwork.orphan_jellyfin.')) return `Jellyfin has a ${d.kind}; Finmeta owns no file for it.`;
  if (f.check_code.startsWith('artwork_quality.tiny_file.')) return `${d.kind} file is suspiciously small — likely truncated.`;
  if (f.check_code.startsWith('artwork_quality.unreadable.')) return `${d.kind} file can't be opened as an image.`;
  if (f.check_code.startsWith('artwork_quality.low_resolution.')) return `${d.kind} is only ${d.width}×${d.height}px (floor: ${d.floor}px wide).`;
  return f.check_code;
}

function severityTone(s: string): BadgeTone {
  if (s === 'error') return 'danger';
  if (s === 'warning') return 'warn';
  return 'neutral';
}

const FIX_LABELS: Record<string, string> = {
  apply_item: 'Fix (apply)',
  refresh_item: 'Refresh in Jellyfin',
};

export function ArtworkPage({ dryRun }: { dryRun: boolean }): React.ReactElement {
  const { push } = useToast();
  const [scanId, setScanId] = useState<string | null | undefined>(undefined); // undefined = loading
  const [findings, setFindings] = useState<ScanFinding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [scanning, setScanning] = useState(false);
  const [fixingAll, setFixingAll] = useState(false);

  const load = useCallback(() => {
    setError(null);
    getLatestScan()
      .then((latest) => {
        setScanId(latest.scan?.scan_id ?? null);
        if (!latest.scan) { setFindings([]); return; }
        return Promise.all([
          getFindings(latest.scan!.scan_id, { check: 'artwork', limit: 200 }),
          getFindings(latest.scan!.scan_id, { check: 'artwork_quality', limit: 200 }),
        ]).then(([a, b]) => {
          const all = [...a.findings, ...b.findings];
          all.sort((x, y) => (x.severity === y.severity ? x.item_name.localeCompare(y.item_name)
            : x.severity === 'error' ? -1 : y.severity === 'error' ? 1
            : x.severity === 'warning' ? -1 : 1));
          setFindings(all);
        });
      })
      .catch((e: any) => setError(e?.message ?? 'Could not load artwork findings.'));
  }, []);

  useEffect(() => { load(); }, [load]);

  const runScan = () => {
    setScanning(true);
    startScan(undefined, 1)
      .then(() => { push({ message: 'Scan started — check Activity for progress.' }); })
      .catch((e: any) => push({ message: e?.message ?? 'Could not start the scan.', tone: 'danger' }))
      .finally(() => setScanning(false));
  };

  const fixable = (f: ScanFinding) => f.fix_kind === 'apply_item' || f.fix_kind === 'refresh_item';

  const submitFix = (ids: number[]) => {
    if (!scanId || ids.length === 0) return;
    fixFindings(scanId, ids)
      .then((r) => {
        const started = r.submitted.filter((s) => s.run_id).length;
        const refreshed = r.submitted.filter((s) => s.action === 'refresh_item' && !s.error).length;
        const failed = r.submitted.filter((s) => s.error).length;
        const parts = [];
        if (started) parts.push(`${started} apply run started`);
        if (refreshed) parts.push(`${refreshed} Jellyfin refresh requested`);
        if (failed) parts.push(`${failed} failed`);
        push({ message: `${parts.join(' · ') || 'Nothing to do'}. Rescan to confirm.` });
        setSelected(new Set());
      })
      .catch((e: any) => push({ message: e?.message ?? 'Could not submit the fix.', tone: 'danger' }))
      .finally(() => setFixingAll(false));
  };

  if (error) return <ErrorState title="Could not load artwork findings" description={error} action={<Button onClick={load}>Retry</Button>} />;

  const loading = scanId === undefined || findings === null;
  const fixableIds = (findings ?? []).filter(fixable).map((f) => f.id);

  return (
    <div>
      <PageHeader
        title="Artwork"
        description={loading ? undefined : `${(findings ?? []).length} finding${(findings ?? []).length === 1 ? '' : 's'} across all libraries`}
        actions={<Button variant="primary" loading={scanning} onClick={runScan}>Rescan</Button>}
      />

      {loading ? (
        <Skeleton height={200} />
      ) : scanId === null ? (
        <EmptyState icon="search" title="No scan yet" description="Run one to see artwork problems here." action={<Button onClick={runScan} loading={scanning}>Scan now</Button>} />
      ) : findings!.length === 0 ? (
        <EmptyState icon="artwork" title="No artwork problems found" description="Every title has a poster, backdrop and logo Jellyfin can see." />
      ) : (
        <>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14 }}>
            <Button
              size="sm"
              disabled={fixableIds.length === 0 || dryRun}
              loading={fixingAll}
              onClick={() => { setFixingAll(true); submitFix(fixableIds); }}
            >
              Fix all {fixableIds.length} automatic
            </Button>
            {dryRun && <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>dry_run is on — fixes are refused until it's off</span>}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {findings!.map((f) => (
              <Card key={f.id} style={{ borderRadius: 0, borderBottom: '1px solid var(--border-subtle)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  {fixable(f) && (
                    <Checkbox
                      checked={selected.has(f.id)}
                      onChange={() => setSelected((prev) => {
                        const next = new Set(prev);
                        if (next.has(f.id)) next.delete(f.id); else next.add(f.id);
                        return next;
                      })}
                      label={`Select ${f.item_name}`}
                    />
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <strong style={{ fontSize: 'var(--text-sm)' }}>{f.item_name}</strong>
                      <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>{f.library}</span>
                      <Badge tone={severityTone(f.severity)}>{f.severity}</Badge>
                    </div>
                    <div style={{ fontSize: 'var(--text-sm)', color: 'var(--text-muted)', marginTop: 2 }}>{describe(f)}</div>
                  </div>
                  <div>
                    {fixable(f) && (
                      <Button
                        size="sm"
                        disabled={f.fix_kind === 'apply_item' && dryRun}
                        onClick={() => submitFix([f.id])}
                      >
                        {FIX_LABELS[f.fix_kind]}
                      </Button>
                    )}
                    {f.fix_kind === 'candidate_picker' && f.item_id && (
                      <Button size="sm" onClick={() => navigate(`/library/${encodeURIComponent(f.library)}/item/${encodeURIComponent(f.item_id!)}`)}>
                        Pick artwork…
                      </Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default ArtworkPage;

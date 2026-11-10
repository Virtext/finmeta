import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Badge, Button, EmptyState, ErrorState, PageHeader, PosterCard, Skeleton, useToast,
} from '../ui';
import {
  bulkRun, getItemsLight, getJob, getLibraries, getPlan, itemImageUrl, reshuffleItem, startRun,
} from '../api';
import type { BulkRunResultEntry } from '../api';
import PreviewPane from '../components/PreviewPane';
import type { ItemLightView, JobResp, LibraryView, PlanResp } from '../types';
import { navigate } from './router';

const PAGE_SIZE = 100;
const TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);

interface ParsedPath { library: string | null; itemId: string | null; }

function parsePath(path: string): ParsedPath {
  const parts = path.split('/').filter(Boolean); // ['library', '<lib>', 'item', '<id>']
  const library = parts[1] ? decodeURIComponent(parts[1]) : null;
  const itemId = parts[2] === 'item' && parts[3] ? decodeURIComponent(parts[3]) : null;
  return { library, itemId };
}

// ---------------------------------------------------------------------------
// Library picker (no library chosen yet)
// ---------------------------------------------------------------------------

function LibraryPicker(): React.ReactElement {
  const [libs, setLibs] = useState<LibraryView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getLibraries().then((r) => setLibs(r.libraries)).catch((e) => setError(e.message));
  }, []);

  if (error) return <ErrorState title="Could not load libraries" description={error} />;
  if (!libs) return <Skeleton height={80} />;
  if (libs.length === 0) return <EmptyState icon="library" title="No libraries configured" />;

  return (
    <div className="fm-grid-3">
      {libs.map((l) => (
        <button
          key={l.name}
          className="fm-card"
          style={{ textAlign: 'left', cursor: 'pointer' }}
          onClick={() => navigate(`/library/${encodeURIComponent(l.name)}`)}
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{l.name}</div>
          <div style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>{l.type}</div>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Poster grid for one library
// ---------------------------------------------------------------------------

interface GridProps {
  library: string;
  selectedId: string | null;
  narrow: boolean;
  /** Bulk selection (S13d) only makes sense browsing the whole library --
   * never in the narrow side-grid next to an already-open item detail. */
  selection?: { ids: Set<string>; onToggle: (id: string) => void };
}

function PosterGrid({ library, selectedId, narrow, selection }: GridProps): React.ReactElement {
  const [items, setItems] = useState<ItemLightView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const load = useCallback((q: string) => {
    setError(null);
    getItemsLight(library, { limit: PAGE_SIZE, offset: 0, search: q || undefined })
      .then((r) => setItems(r.items))
      .catch((e) => setError(e.message));
  }, [library]);

  useEffect(() => { setItems(null); load(search); }, [library]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <ErrorState title="Could not load this library" description={error} />;

  return (
    <div>
      {!narrow && (
        <div style={{ marginBottom: 14 }}>
          <input
            className="fm-input"
            placeholder={`Search ${library}…`}
            style={{ maxWidth: 320 }}
            value={search}
            onChange={(e) => { setSearch(e.target.value); load(e.target.value); }}
          />
        </div>
      )}
      {items === null ? (
        <div style={{ display: 'grid', gridTemplateColumns: `repeat(${narrow ? 2 : 6}, minmax(0,1fr))`, gap: 12 }}>
          {Array.from({ length: narrow ? 6 : 18 }).map((_, i) => <Skeleton key={i} height={narrow ? 140 : 190} radius="var(--r-md)" />)}
        </div>
      ) : items.length === 0 ? (
        <EmptyState icon="search" title="No titles match" description={search ? `Nothing matches "${search}".` : 'This library has no titles yet.'} />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? 'repeat(2, minmax(0,1fr))' : 'repeat(auto-fill, minmax(130px, 1fr))', gap: 12 }}>
          {items.map((it) => (
            <PosterCard
              key={it.item_id}
              title={it.item_name}
              imageUrl={itemImageUrl(it.item_id, 'poster')}
              selected={selection ? selection.ids.has(it.item_id) : it.item_id === selectedId}
              selectable={!!selection}
              onToggleSelect={selection ? () => selection.onToggle(it.item_id) : undefined}
              onClick={() => navigate(`/library/${encodeURIComponent(library)}/item/${encodeURIComponent(it.item_id)}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bulk selection bar (S13d): Plan/Apply route through the real bulk-run
// endpoint (the single run slot's FIFO, same mechanism the scan-findings
// "fix all" already uses); "Find new artwork" just reshuffles each item's
// poster candidate -- no run slot involved, nothing written until the next
// apply. No "Lock artwork" / "Set overlay profile" / "Force re-identify"
// here: none of those have a real per-item backend operation to call (locks
// are a side effect of a successful apply, not a standalone action;
// profiles are per-LIBRARY not per-item; there is no manual re-identify
// override) -- offering buttons for things that don't exist would be a
// fake affordance.
// ---------------------------------------------------------------------------

function BulkActionBar({
  library, dryRun, selectedIds, onClear,
}: { library: string; dryRun: boolean; selectedIds: Set<string>; onClear: () => void }): React.ReactElement {
  const { push } = useToast();
  const [busy, setBusy] = useState<'plan' | 'apply' | 'reshuffle' | null>(null);
  const ids = [...selectedIds];

  const summarize = (results: BulkRunResultEntry[], verb: string) => {
    const started = results.filter((r) => r.run_id).length;
    const queued = results.filter((r) => r.queued).length;
    const failed = results.filter((r) => !r.run_id && !r.queued).length;
    const parts: string[] = [];
    if (started) parts.push(`${started} ${verb} started`);
    if (queued) parts.push(`${queued} queued`);
    if (failed) parts.push(`${failed} could not start`);
    push({ message: parts.join(' · ') || 'Nothing to do', tone: failed ? 'warn' : 'ok' });
  };

  const runBulk = (action: 'plan' | 'apply') => {
    setBusy(action);
    bulkRun(action, ids, library)
      .then((r) => { summarize(r.results, action); onClear(); })
      .catch((e: any) => push({ message: e?.message ?? `Could not ${action} the selection.`, tone: 'danger' }))
      .finally(() => setBusy(null));
  };

  const findNewArtwork = () => {
    setBusy('reshuffle');
    Promise.allSettled(ids.map((id) => reshuffleItem(id, ['poster'])))
      .then((results) => {
        const ok = results.filter((r) => r.status === 'fulfilled').length;
        const failed = results.length - ok;
        push({ message: `${ok} reshuffled${failed ? ` · ${failed} failed` : ''}. Plan or apply to see the new pick.` });
        onClear();
      })
      .finally(() => setBusy(null));
  };

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
      background: 'var(--accent-weak)', border: '1px solid rgba(0,164,220,0.35)',
      borderRadius: 'var(--r-lg)', padding: '8px 12px', marginBottom: 16,
    }}>
      <Badge tone="accent">{selectedIds.size} selected</Badge>
      <Button size="sm" loading={busy === 'plan'} disabled={busy !== null} onClick={() => runBulk('plan')}>
        Plan selected
      </Button>
      <Button
        size="sm" variant="primary" loading={busy === 'apply'} disabled={busy !== null || dryRun}
        title={dryRun ? 'dry_run is true — set it false in config to apply' : undefined}
        onClick={() => runBulk('apply')}
      >
        Apply selected
      </Button>
      <Button size="sm" loading={busy === 'reshuffle'} disabled={busy !== null} onClick={findNewArtwork}>
        Find new artwork
      </Button>
      <Button size="sm" variant="ghost" disabled={busy !== null} onClick={onClear}>Clear</Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Item detail: real plan + candidates + apply, via the shared PreviewPane
// ---------------------------------------------------------------------------

function ItemDetail({ library, itemId, dryRun }: { library: string; itemId: string; dryRun: boolean }): React.ReactElement {
  const [item, setItem] = useState<ItemLightView | null>(null);
  const [plan, setPlan] = useState<PlanResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [applyJob, setApplyJob] = useState<JobResp | null>(null);
  const [applyRunning, setApplyRunning] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const refreshPlan = useCallback(() => {
    setLoading(true);
    setError(null);
    getPlan(itemId)
      .then((p) => { setPlan(p); setItem({ item_id: p.item_id, item_name: p.item_name, media_type: p.media_type as 'movie' | 'tvshow' }); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [itemId]);

  useEffect(() => {
    refreshPlan();
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [refreshPlan]);

  const startApply = useCallback(() => {
    setApplyRunning(true);
    setApplyError(null);
    setApplyJob(null);
    startRun('apply', itemId, library)
      .then((r) => {
        setApplyJob({ run_id: r.job_id, status: 'running', started_at: null, finished_at: null, files_written: 0, files_unchanged: 0, api_calls: 0, log: [] });
        pollRef.current = window.setInterval(async () => {
          try {
            const j = await getJob(r.job_id);
            setApplyJob(j);
            if (TERMINAL.has(j.status)) {
              if (pollRef.current) window.clearInterval(pollRef.current);
              setApplyRunning(false);
              refreshPlan();
            }
          } catch (e: any) {
            if (pollRef.current) window.clearInterval(pollRef.current);
            setApplyError(e?.message ?? 'Lost track of the apply job.');
            setApplyRunning(false);
          }
        }, 1500);
      })
      .catch((e: any) => { setApplyError(e?.message ?? 'Could not start apply.'); setApplyRunning(false); });
  }, [itemId, library, refreshPlan]);

  if (error) return <ErrorState title="Could not load this item" description={error} action={<Button onClick={refreshPlan}>Retry</Button>} />;

  return (
    <PreviewPane
      item={item}
      plan={plan}
      loading={loading}
      error={null}
      dryRun={dryRun}
      library={library}
      onCandidateSelected={refreshPlan}
      onSkipChanged={refreshPlan}
      onApplyItem={startApply}
      itemApplyJob={applyJob}
      itemApplyRunning={applyRunning}
      itemApplyError={applyError}
    />
  );
}

export interface LibraryPageProps { path: string; dryRun: boolean; }

export function LibraryPage({ path, dryRun }: LibraryPageProps): React.ReactElement {
  const { library, itemId } = parsePath(path);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Selection is per-library and makes no sense carried into an item's own
  // detail view -- clear it whenever either changes.
  useEffect(() => { setSelectedIds(new Set()); }, [library, itemId]);

  if (!library) {
    return (
      <div>
        <PageHeader title="Library" description="Choose a library to browse." />
        <LibraryPicker />
      </div>
    );
  }

  const toggleSelect = (id: string) => setSelectedIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div>
      <PageHeader
        crumb={itemId ? `Library › ${library}` : 'Library'}
        title={library}
        actions={<Button variant="ghost" onClick={() => navigate(itemId ? `/library/${encodeURIComponent(library)}` : '/library')}>
          {itemId ? `← Back to ${library}` : '← All libraries'}
        </Button>}
      />
      {itemId ? (
        <div className="fm-stack-on-mobile" style={{ display: 'grid', gridTemplateColumns: '260px 1fr', gap: 20, alignItems: 'start' }}>
          <PosterGrid library={library} selectedId={itemId} narrow />
          <ItemDetail library={library} itemId={itemId} dryRun={dryRun} />
        </div>
      ) : (
        <>
          {selectedIds.size > 0 && (
            <BulkActionBar
              library={library}
              dryRun={dryRun}
              selectedIds={selectedIds}
              onClear={() => setSelectedIds(new Set())}
            />
          )}
          <PosterGrid
            library={library}
            selectedId={null}
            narrow={false}
            selection={{ ids: selectedIds, onToggle: toggleSelect }}
          />
        </>
      )}
    </div>
  );
}

export default LibraryPage;

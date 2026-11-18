import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Badge, Button, Checkbox, EmptyState, ErrorState, PageHeader, PosterCard, Select, Skeleton, useToast,
} from '../ui';
import {
  bulkRun, getItemsLight, getJob, getLibraries, getPlan, itemImageUrl, reshuffleItem, startRun,
} from '../api';
import type { BulkRunResultEntry } from '../api';
import PreviewPane from '../components/PreviewPane';
import type { ItemLightView, JobResp, LibraryView, PlanResp } from '../types';
import { navigate } from './router';

const BULK_CHUNK = 50; // = backend MAX_BULK_ITEMS
const TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);
// Audit 2026-11-11 "C6": ui/src/components/LibraryBrowser.tsx's own
// debounce convention (the legacy shell), ported here -- this modern
// PosterGrid had regressed to firing one request per keystroke.
const SEARCH_DEBOUNCE_MS = 300;

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

const PAGE_SIZES = [25, 50, 75, 100];
const SORTS = [
  { value: 'name', label: 'Title' },
  { value: 'year', label: 'Year' },
  { value: 'added', label: 'Date added' },
  { value: 'released', label: 'Release date' },
  { value: 'rating', label: 'Rating' },
];
// Per-viewer conveniences only -- storage can be missing or throw.
const PREFS_KEY = 'finmeta.libraryGrid';
function loadPrefs(): { pageSize: number; sort: string; order: 'asc' | 'desc' } {
  const d = { pageSize: 50, sort: 'name', order: 'asc' as 'asc' | 'desc' };
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    return {
      pageSize: PAGE_SIZES.includes(p.pageSize) ? p.pageSize : d.pageSize,
      sort: SORTS.some((o) => o.value === p.sort) ? p.sort : d.sort,
      order: p.order === 'desc' ? 'desc' : 'asc',
    };
  } catch { return d; }
}

interface Selection {
  ids: Set<string>;
  /** "All N titles in this library" mode -- a library-wide run, not an id list. */
  allLibrary: boolean;
  onToggle: (id: string) => void;
  onSetMany: (ids: string[], on: boolean) => void;
  onSelectAllLibrary: (total: number) => void;
}

interface GridProps {
  library: string;
  selectedId: string | null;
  narrow: boolean;
  /** Bulk selection (S13d) only makes sense browsing the whole library --
   * never in the narrow side-grid next to an already-open item detail. */
  selection?: Selection;
}

function PosterGrid({ library, selectedId, narrow, selection }: GridProps): React.ReactElement {
  const [items, setItems] = useState<ItemLightView[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState(''); // debounced copy of `search`
  const [page, setPage] = useState(0);
  const [prefs, setPrefs] = useState(loadPrefs);
  const { pageSize, sort, order } = prefs;
  // Audit 2026-11-11 "C6": a fast typer or a slow response used to leave
  // the grid showing an earlier keystroke's results -- bumped before
  // every load; a response is only applied if it's still the most recent
  // one in flight (also covers a library switch landing mid-request).
  const reqIdRef = useRef(0);

  const setPref = (patch: Partial<typeof prefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    setPage(0);
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
  };

  useEffect(() => { setSearch(''); setQuery(''); setPage(0); }, [library]);

  useEffect(() => {
    setError(null);
    setItems(null);
    const reqId = ++reqIdRef.current;
    getItemsLight(library, { limit: pageSize, offset: page * pageSize, search: query || undefined, sort, order })
      .then((r) => { if (reqIdRef.current === reqId) { setItems(r.items); setTotal(r.total_items); } })
      .catch((e) => { if (reqIdRef.current === reqId) setError(e.message); });
  }, [library, query, page, pageSize, sort, order]);

  // Debounced server-side search (ported from the legacy LibraryBrowser,
  // see SEARCH_DEBOUNCE_MS above): typing updates the input immediately,
  // but the Jellyfin-backed request only fires after a pause.
  useEffect(() => {
    if (search === query) return undefined;
    const t = window.setTimeout(() => { setQuery(search); setPage(0); }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <ErrorState title="Could not load this library" description={error} />;

  const pages = Math.max(1, Math.ceil(total / pageSize));
  const pageIds = (items ?? []).map((it) => it.item_id);
  const pageAllSelected = !!selection && pageIds.length > 0
    && (selection.allLibrary || pageIds.every((id) => selection.ids.has(id)));

  const pager = (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'center', marginTop: 16, flexWrap: 'wrap' }}>
      <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage(0)} aria-label="First page">«</Button>
      <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>‹ Prev</Button>
      <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>
        Page {page + 1} of {pages}{narrow ? '' : ` · ${total} titles`}
      </span>
      <Button size="sm" variant="ghost" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next ›</Button>
      <Button size="sm" variant="ghost" disabled={page >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="Last page">»</Button>
    </div>
  );

  return (
    <div>
      {!narrow && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
          {selection && (
            <Checkbox
              checked={pageAllSelected}
              label="Select all on this page"
              disabled={pageIds.length === 0}
              onChange={(on) => selection.onSetMany(pageIds, on)}
            />
          )}
          <input
            className="fm-input"
            placeholder={`Search ${library}…`}
            style={{ maxWidth: 280 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>Sort</span>
            <Select aria-label="Sort by" options={SORTS} value={sort} onChange={(e) => setPref({ sort: e.target.value })} />
            <Button
              size="sm" variant="ghost"
              aria-label={order === 'asc' ? 'Ascending' : 'Descending'}
              title={order === 'asc' ? 'Ascending' : 'Descending'}
              onClick={() => setPref({ order: order === 'asc' ? 'desc' : 'asc' })}
            >{order === 'asc' ? '↑' : '↓'}</Button>
            <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text-faint)' }}>Show</span>
            <Select
              aria-label="Titles per page"
              options={PAGE_SIZES.map((n) => ({ value: String(n), label: String(n) }))}
              value={String(pageSize)}
              onChange={(e) => setPref({ pageSize: Number(e.target.value) })}
            />
          </div>
        </div>
      )}
      {selection && pageAllSelected && !selection.allLibrary && total > pageIds.length && (
        <div style={{ fontSize: 'var(--text-sm)', marginBottom: 12 }}>
          All {pageIds.length} on this page selected.{' '}
          <Button size="sm" variant="ghost" onClick={() => selection.onSelectAllLibrary(total)}>
            Select all {total} titles in {library}
          </Button>
        </div>
      )}
      {items === null ? (
        <div style={{ display: 'grid', gridTemplateColumns: `repeat(${narrow ? 2 : 6}, minmax(0,1fr))`, gap: 12 }}>
          {Array.from({ length: narrow ? 6 : 18 }).map((_, i) => <Skeleton key={i} height={narrow ? 140 : 190} radius="var(--r-md)" />)}
        </div>
      ) : items.length === 0 ? (
        <EmptyState icon="search" title="No titles match" description={query ? `Nothing matches "${query}".` : 'This library has no titles yet.'} />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? 'repeat(2, minmax(0,1fr))' : 'repeat(auto-fill, minmax(130px, 1fr))', gap: 12 }}>
          {items.map((it) => (
            <PosterCard
              key={it.item_id}
              title={it.item_name}
              meta={it.year ? String(it.year) : undefined}
              imageUrl={itemImageUrl(it.item_id, 'poster', 320, it.poster_tag)}
              selected={selection ? selection.allLibrary || selection.ids.has(it.item_id) : it.item_id === selectedId}
              selectable={!!selection}
              onToggleSelect={selection ? () => selection.onToggle(it.item_id) : undefined}
              onClick={() => navigate(`/library/${encodeURIComponent(library)}/item/${encodeURIComponent(it.item_id)}`)}
            />
          ))}
        </div>
      )}
      {total > pageSize && pager}
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
  library, dryRun, selectedIds, allLibrary, onClear,
}: {
  library: string; dryRun: boolean; selectedIds: Set<string>;
  /** Total title count when "all in library" is selected, else null. */
  allLibrary: number | null; onClear: () => void;
}): React.ReactElement {
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
    if (allLibrary !== null) {
      // Whole library = one library-wide background run, not N item runs.
      startRun(action, undefined, library)
        .then(() => { push({ message: `Library ${action} started — follow it in Activity.`, tone: 'ok' }); onClear(); })
        .catch((e: any) => push({ message: e?.message ?? `Could not ${action} the library.`, tone: 'danger' }))
        .finally(() => setBusy(null));
      return;
    }
    // The endpoint caps one request at MAX_BULK_ITEMS (50) and asks for
    // batches; send them one after another so apply's FIFO keeps order.
    const chunks: string[][] = [];
    for (let i = 0; i < ids.length; i += BULK_CHUNK) chunks.push(ids.slice(i, i + BULK_CHUNK));
    chunks.reduce<Promise<BulkRunResultEntry[]>>(
      (acc, chunk) => acc.then((all) => bulkRun(action, chunk, library).then((r) => [...all, ...r.results])),
      Promise.resolve([]),
    )
      .then((results) => { summarize(results, action); onClear(); })
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
      <Badge tone="accent">{allLibrary !== null ? `All ${allLibrary} titles` : `${selectedIds.size} selected`}</Badge>
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
      <Button
        size="sm" loading={busy === 'reshuffle'} disabled={busy !== null || allLibrary !== null}
        title={allLibrary !== null ? 'Select titles on a page to reshuffle their artwork' : undefined}
        onClick={findNewArtwork}
      >
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
  const [allLibrary, setAllLibrary] = useState<number | null>(null);
  const clearSelection = () => { setSelectedIds(new Set()); setAllLibrary(null); };

  // Selection is per-library and makes no sense carried into an item's own
  // detail view -- clear it whenever either changes.
  useEffect(clearSelection, [library, itemId]);

  if (!library) {
    return (
      <div>
        <PageHeader title="Library" description="Choose a library to browse." />
        <LibraryPicker />
      </div>
    );
  }

  const toggleSelect = (id: string) => {
    // Unticking one title leaves "all in library" mode for an explicit list.
    if (allLibrary !== null) { setAllLibrary(null); setSelectedIds(new Set()); return; }
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const setMany = (ids: string[], on: boolean) => {
    setAllLibrary(null);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => (on ? next.add(id) : next.delete(id)));
      return next;
    });
  };

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
          {(selectedIds.size > 0 || allLibrary !== null) && (
            <BulkActionBar
              library={library}
              dryRun={dryRun}
              selectedIds={selectedIds}
              allLibrary={allLibrary}
              onClear={clearSelection}
            />
          )}
          <PosterGrid
            library={library}
            selectedId={null}
            narrow={false}
            selection={{
              ids: selectedIds, allLibrary: allLibrary !== null, onToggle: toggleSelect,
              onSetMany: setMany, onSelectAllLibrary: setAllLibrary,
            }}
          />
        </>
      )}
    </div>
  );
}

export default LibraryPage;

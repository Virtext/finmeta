import { useCallback, useEffect, useRef, useState } from 'react';
import { getAuthStatus, getItemsLight, getLibraries, getPlan, startRun, getJob, getHealth } from './api';
import type { JobResp } from './types';
import type { ItemLightView, LibItemsLightResp, LibrariesResp, PlanResp } from './types';
import LibraryBrowser from './components/LibraryBrowser';
import { EMPTY_LIBRARY_RUN, type LibraryRunState } from './components/BulkApplyPanel';
import PreviewPane from './components/PreviewPane';
import LoginScreen from './components/LoginScreen';
import SettingsPage from './components/SettingsPage';
import AccountPage from './components/AccountPage';
import { logout } from './api';
import ErrorBoundary from './ErrorBoundary';

type Page = 'main' | 'settings' | 'account';

// Pagination fix (2026-10-30): /items/light pages at Jellyfin now (bounded
// 1-500 server-side) -- this is just the UI's own "Load more" chunk size.
const LIBRARY_PAGE_SIZE = 200;

// Shared by the topbar's "Apply all libraries" run.
const ALL_RUN_TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);

export default function App() {
  const [unauthed, setUnauthed] = useState(false);
  const [page, setPage] = useState<Page>('main');
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  // S9d4c Fase A step 4: "Edit overlay profile" from PreviewPane opens
  // Settings straight to the Overlays tab with this library preselected.
  const [overlaySettingsLib, setOverlaySettingsLib] = useState<string | null>(null);
  const openOverlayEditor = useCallback((lib: string) => {
    setOverlaySettingsLib(lib);
    setPage('settings');
  }, []);

  // Lys/mørk tema: default follows prefers-color-scheme; choice is persisted in
  // localStorage (guarded — some contexts throw on access).
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      const stored = localStorage.getItem('finmeta-theme');
      if (stored === 'light' || stored === 'dark') return stored;
    } catch { /* ignore */ }
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  });
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('finmeta-theme', theme); } catch { /* ignore */ }
  }, [theme]);

  const [libs, setLibs] = useState<LibrariesResp | null>(null);
  const [libName, setLibName] = useState<string>('');
  const [items, setItems] = useState<LibItemsLightResp | null>(null);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [itemsLoadingMore, setItemsLoadingMore] = useState(false);
  const [itemsError, setItemsError] = useState<string | null>(null);
  const [itemsSearch, setItemsSearch] = useState('');
  // Invalidates an in-flight search/page response that a newer one has
  // already superseded (fast typing, or switching libraries mid-request).
  const itemsReqIdRef = useRef(0);

  const [selected, setSelected] = useState<ItemLightView | null>(null);
  const [plan, setPlan] = useState<PlanResp | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  // S9d4b step 6: item-scoped apply (scope item:<id>).
  const [itemApplyJob, setItemApplyJob] = useState<JobResp | null>(null);
  const [itemApplyRunning, setItemApplyRunning] = useState(false);
  const [itemApplyError, setItemApplyError] = useState<string | null>(null);

  // "Apply all libraries": one run with no library/item scope. This is the
  // same full scope CollectionsPanel's own "Apply (all libraries)" button
  // already triggers (presets/seasonal collections only ever build on it --
  // see that component) -- surfaced here too since most applies aren't
  // about collections and clicking through to Settings -> Collections just
  // to apply everything at once isn't obvious. Lives in the topbar next to
  // the dry-run badge rather than as one more per-library button. No
  // reload-resume for this scope (same gap already exists for Collections'
  // button); it is a short-lived in-memory state like the rest of this file.
  const [allRunJob, setAllRunJob] = useState<JobResp | null>(null);
  const [allRunning, setAllRunning] = useState(false);
  const [allRunErr, setAllRunErr] = useState<string | null>(null);
  const allRunTimer = useRef<number | null>(null);
  useEffect(() => () => { if (allRunTimer.current) window.clearInterval(allRunTimer.current); }, []);

  const startApplyAll = useCallback(() => {
    setAllRunning(true); setAllRunErr(null); setAllRunJob(null);
    startRun('apply').then((r) => {
      allRunTimer.current = window.setInterval(async () => {
        try {
          const j = await getJob(r.job_id);
          setAllRunJob(j);
          if (ALL_RUN_TERMINAL.has(j.status)) {
            if (allRunTimer.current) window.clearInterval(allRunTimer.current);
            setAllRunning(false);
          }
        } catch (e) {
          if (allRunTimer.current) window.clearInterval(allRunTimer.current);
          setAllRunErr((e as Error).message);
          setAllRunning(false);
        }
      }, 1500);
    }).catch((e) => { setAllRunErr((e as Error).message); setAllRunning(false); });
  }, []);

  // S9d2: library-wide plan/apply, keyed by library name (not local to
  // BulkApplyPanel) -- switching library tabs flips itemsLoading=true for a
  // moment, which unmounts BulkApplyPanel. A poll timer owned by that
  // component used to die with it, so navigating away from an in-flight
  // library run made it look stopped even though the backend worker thread
  // (run_service.py) keeps going regardless of what the UI does.
  const [libraryRuns, setLibraryRuns] = useState<Record<string, LibraryRunState>>({});
  const libraryRunTimers = useRef<Record<string, number>>({});
  useEffect(() => () => {
    Object.values(libraryRunTimers.current).forEach((id) => window.clearInterval(id));
  }, []);

  // Job status lost on page reload: a run started from a previous page load
  // keeps going on the backend (run_service._worker) regardless of what the
  // UI does -- only the frontend's own in-memory libraryRuns/itemApplyJob
  // state forgot it ever existed. Holds an item-scoped run until that
  // library's `items` have loaded and the target item can actually be
  // selected (see the effect below); a library-scoped run is resumed
  // immediately since it needs no such wait.
  const pendingResumeItem = useRef<{ library: string | null; itemId: string; jobId: string } | null>(null);
  // Indirection so loadLibs (defined before the library-run poller exists)
  // can resume into it without widening loadLibs's own dependency array --
  // kept current every render, read only from inside an already-deferred
  // callback (loadLibs's own .then, which never runs during this render).
  const resumeLibraryRunRef = useRef<((library: string, jobId: string) => void) | null>(null);

  const loadLibs = useCallback(() => {
    getLibraries()
      .then((d) => {
        setLibs(d);
        if (d.libraries.length) setLibName(d.libraries[0].name);
        return getHealth().catch(() => null);
      })
      .then((h) => {
        if (!h?.active_run || !h.active_run_scope) return;
        const scope = h.active_run_scope;
        if (scope.item_id) {
          pendingResumeItem.current = { library: scope.library, itemId: scope.item_id, jobId: h.active_run! };
          if (scope.library) setLibName(scope.library);
        } else if (scope.library) {
          setLibraryRuns((prev) => ({
            ...prev,
            [scope.library!]: {
              job: { run_id: h.active_run!, status: 'running', started_at: null, finished_at: null,
                     files_written: 0, files_unchanged: 0, api_calls: 0, log: [] },
              action: (scope.action === 'plan' ? 'plan' : 'apply'), running: true, error: null,
            },
          }));
          resumeLibraryRunRef.current?.(scope.library, h.active_run!);
        }
      })
      .catch((e: any) => {
        // ANY 401 from the Finmeta API = expired/invalid session -> show login.
        // 502/500 etc. keep the session and show an inline error.
        if (e?.isUnauthorized) setUnauthed(true);
        else setFatal(e.message);
      });
  }, []);

  // On mount: check auth status, then load libraries.
  useEffect(() => {
    getAuthStatus()
      .then((d) => {
        if (d.auth_enabled && !d.has_password) {
          setUnauthed(true);
        } else {
          loadLibs();
        }
      })
      .catch(() => {
        // 401 on auth/status shouldn't happen (it's public), but just in case
        loadLibs();
      });
  }, [loadLibs]);

  // Items for the current library: Jellyfin-only (names, no TMDb/fanart/etc.
  // lookups), so this is fast even for hundreds of titles -- building a plan
  // for every item just to list them was profiled at ~7s/item cold, which
  // no amount of pagination made feel fast. The list exists so you can find
  // and select a title; the per-item plan/diff/status is still fetched on
  // demand for whichever one you actually select (see refreshPlan below).
  const loadLibraryItems = useCallback((
    name: string, isCancelled: () => boolean, onComplete?: (d: LibItemsLightResp) => void,
    search?: string,
  ) => {
    setItemsLoading(true);
    setItemsError(null);
    getItemsLight(name, { limit: LIBRARY_PAGE_SIZE, offset: 0, search: search || undefined })
      .then((d) => {
        if (isCancelled()) return;
        setItems(d);
        onComplete?.(d);
      })
      .catch((e: any) => {
        if (isCancelled()) return;
        if (e?.isUnauthorized) setUnauthed(true);
        else setItemsError(e.message);
      })
      .finally(() => { if (!isCancelled()) setItemsLoading(false); });
  }, []);

  useEffect(() => {
    if (!libName || unauthed) return;
    setItemsSearch('');
    const reqId = ++itemsReqIdRef.current;
    loadLibraryItems(libName, () => itemsReqIdRef.current !== reqId, (d) => {
      setSelected((prev) => (prev && d.items.some((i) => i.item_id === prev.item_id) ? prev : null));
    });
  }, [libName, unauthed, loadLibraryItems]);

  // Server-side search (Jellyfin SearchTerm via /items/light?search=) --
  // LibraryBrowser debounces keystrokes before calling this, so typing
  // doesn't fire a request per character. Replaces the current page with
  // the first page of matches; clearing the query reloads the unfiltered
  // library the same way.
  const handleSearch = useCallback((query: string) => {
    setItemsSearch(query);
    if (!libName) return;
    const reqId = ++itemsReqIdRef.current;
    loadLibraryItems(libName, () => itemsReqIdRef.current !== reqId, undefined, query);
  }, [libName, loadLibraryItems]);

  // "Load more": fetch the next page and append -- the list never refetches
  // pages it already has.
  const loadMoreItems = useCallback(() => {
    if (!libName || !items || !items.has_more || itemsLoadingMore) return;
    const reqId = ++itemsReqIdRef.current;
    setItemsLoadingMore(true);
    getItemsLight(libName, {
      limit: LIBRARY_PAGE_SIZE, offset: items.items.length, search: itemsSearch || undefined,
    })
      .then((d) => {
        if (itemsReqIdRef.current !== reqId) return;
        setItems((prev) => (prev ? { ...d, items: [...prev.items, ...d.items] } : d));
      })
      .catch((e: any) => {
        if (itemsReqIdRef.current !== reqId) return;
        if (e?.isUnauthorized) setUnauthed(true);
        else setItemsError(e.message);
      })
      .finally(() => { if (itemsReqIdRef.current === reqId) setItemsLoadingMore(false); });
  }, [libName, items, itemsLoadingMore, itemsSearch]);

  // Switching selection fast (e.g. Breaking Bad -> The Office before Breaking
  // Bad's plan has even come back) used to let whichever response arrived
  // LAST win, regardless of which one was still selected by then -- title
  // says "The Office" but the poster/candidates are Breaking Bad's. Guard
  // against it the same way: only apply a response if its own item_id still
  // matches the most recently requested one.
  const latestPlanRequest = useRef<string | null>(null);
  const refreshPlan = useCallback((id: string) => {
    latestPlanRequest.current = id;
    setPlanLoading(true);
    setPlanError(null);
    getPlan(id)
      .then((d) => { if (latestPlanRequest.current === id) setPlan(d); })
      .catch((e) => { if (latestPlanRequest.current === id) setPlanError(e.message); })
      .finally(() => { if (latestPlanRequest.current === id) setPlanLoading(false); });
  }, []);

  useEffect(() => {
    setPlan(null);
    if (selected) refreshPlan(selected.item_id);
  }, [selected, refreshPlan]);

  const onRunDone = useCallback(() => {
    if (libName) {
      loadLibraryItems(libName, () => false, (d) => {
        setSelected((prev) => (prev ? d.items.find((i) => i.item_id === prev.item_id) ?? prev : prev));
      });
    }
    if (selected) refreshPlan(selected.item_id);
  }, [libName, selected, refreshPlan, loadLibraryItems]);

  const TERMINAL = new Set(['ok', 'partial', 'error', 'refused']);

  // Shared by startLibraryRun (fresh submit) and the mount-time resume path
  // (reconnecting to a run that was already going when the page loaded) --
  // both just need to poll an existing job_id into libraryRuns[library].
  const pollLibraryRun = useCallback((library: string, jobId: string) => {
    const iv = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setLibraryRuns((prev) => ({ ...prev, [library]: { ...prev[library], job: j } }));
        if (TERMINAL.has(j.status)) {
          window.clearInterval(iv);
          delete libraryRunTimers.current[library];
          setLibraryRuns((prev) => ({ ...prev, [library]: { ...prev[library], running: false } }));
          if (library === libName) onRunDone();
        }
      } catch (e) {
        window.clearInterval(iv);
        delete libraryRunTimers.current[library];
        setLibraryRuns((prev) => ({
          ...prev, [library]: { ...prev[library], running: false, error: (e as Error).message },
        }));
      }
    }, 1500);
    libraryRunTimers.current[library] = iv;
  }, [libName, onRunDone]);
  resumeLibraryRunRef.current = pollLibraryRun;

  const startLibraryRun = useCallback(async (library: string, kind: 'plan' | 'apply') => {
    setLibraryRuns((prev) => ({ ...prev, [library]: { job: null, action: kind, running: true, error: null } }));
    try {
      const r = await startRun(kind, undefined, library);
      setLibraryRuns((prev) => ({
        ...prev,
        [library]: {
          job: { run_id: r.job_id, status: 'running', started_at: null, finished_at: null,
                 files_written: 0, files_unchanged: 0, api_calls: 0, log: [] },
          action: kind, running: true, error: null,
        },
      }));
      pollLibraryRun(library, r.job_id);
    } catch (e) {
      setLibraryRuns((prev) => ({
        ...prev, [library]: { job: null, action: kind, running: false, error: (e as Error).message },
      }));
    }
  }, [pollLibraryRun]);

  // Shared by startItemApply (fresh submit) and the mount-time resume path.
  const pollItemJob = useCallback((jobId: string) => {
    const iv = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setItemApplyJob(j);
        if (TERMINAL.has(j.status)) {
          window.clearInterval(iv);
          setItemApplyRunning(false);
          onRunDone();
        }
      } catch (e) {
        window.clearInterval(iv);
        setItemApplyError((e as Error).message);
        setItemApplyRunning(false);
      }
    }, 1500);
  }, [onRunDone]);

  // S9d4b step 6: apply only the selected item (scope item:<id>).
  const startItemApply = useCallback(async () => {
    if (!selected) return;
    setItemApplyRunning(true);
    setItemApplyError(null);
    setItemApplyJob(null);
    try {
      const r = await startRun('apply', selected.item_id, libName || undefined);
      const jobId = r.job_id;
      setItemApplyJob({ run_id: jobId, status: 'running', started_at: null, finished_at: null, files_written: 0, files_unchanged: 0, api_calls: 0, log: [] });
      pollItemJob(jobId);
    } catch (e) {
      setItemApplyError((e as Error).message);
      setItemApplyRunning(false);
    }
  }, [selected, libName, pollItemJob]);

  // Job status lost on page reload, item-scoped half: loadLibs's resume
  // check (see above) can only record WHICH item/job to resume into --
  // actually selecting it has to wait until that library's items have
  // loaded, which happens asynchronously via the libName effect above.
  useEffect(() => {
    const pending = pendingResumeItem.current;
    if (!pending || !items) return;
    if (pending.library && pending.library !== libName) return;
    pendingResumeItem.current = null;
    const found = items.items.find((i) => i.item_id === pending.itemId);
    if (found) setSelected(found);
    setItemApplyRunning(true);
    setItemApplyJob({ run_id: pending.jobId, status: 'running', started_at: null, finished_at: null,
                      files_written: 0, files_unchanged: 0, api_calls: 0, log: [] });
    pollItemJob(pending.jobId);
  }, [items, libName, pollItemJob]);

  // Early return: login screen.
  if (unauthed) {
    return <LoginScreen onAuthed={() => { setUnauthed(false); loadLibs(); }} />;
  }

  // Early return: settings page.
  if (page === 'settings') {
    return (
      <SettingsPage
        onBack={() => setPage('main')}
        initialTab={overlaySettingsLib ? 'overlays' : undefined}
        initialOverlayLibrary={overlaySettingsLib}
      />
    );
  }

  // Early return: account page (username/password change).
  if (page === 'account') {
    return <AccountPage onBack={() => setPage('main')} />;
  }

  const dryRun = libs?.dry_run ?? true;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/finmeta-icon.svg" alt="" className="brand-icon" />
          Finmeta
        </div>
        <nav className="lib-tabs">
          {libs?.libraries.map((l) => (
            <button
              key={l.name}
              className={`lib-tab ${l.name === libName ? 'active' : ''}`}
              onClick={() => setLibName(l.name)}
            >
              {l.name}
              <span className="lib-type muted">{l.type}</span>
            </button>
          ))}
        </nav>
        <div className={`dryflag ${dryRun ? 'on' : 'off'}`}>{dryRun ? 'DRY RUN' : 'LIVE'}</div>
        <div className="apply-all">
          <button
            className="btn primary"
            disabled={allRunning || dryRun}
            title={dryRun ? 'dry_run is true — set it false in config to apply' : 'Apply every configured library in one run'}
            onClick={() => {
              if (window.confirm('Apply ALL configured libraries now? Use "Plan library" per library first to see what will change.')) {
                startApplyAll();
              }
            }}
          >
            {allRunning ? 'Applying all…' : 'Apply all libraries'}
          </button>
          {allRunJob && ALL_RUN_TERMINAL.has(allRunJob.status) && (
            <span className={`muted apply-all-result ${allRunJob.status}`}>
              {allRunJob.status} · {allRunJob.files_written} written · {allRunJob.files_unchanged} unchanged
            </span>
          )}
          {allRunErr && <span className="field-msg err">{allRunErr}</span>}
        </div>
        <button className="btn settings-btn" onClick={() => setPage('settings')}>⚙ Settings</button>
        <button
          className="theme-toggle user-icon"
          title={theme === 'dark' ? 'Lys tema' : 'Mørk tema'}
          aria-label={theme === 'dark' ? 'Bytt til lyst tema' : 'Bytt til mørkt tema'}
          onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
        >
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
        <div className="user-menu">
          <button
            className="user-icon"
            title="Account"
            aria-label="Account menu"
            onClick={() => setUserMenuOpen((v) => !v)}
          >
            👤
          </button>
          {userMenuOpen && (
            <>
              <div className="user-menu-backdrop" onClick={() => setUserMenuOpen(false)} />
              <div className="user-menu-list">
                <button onClick={() => { setUserMenuOpen(false); setPage('account'); }}>Account</button>
                <button onClick={() => { logout().catch(() => {}); setUnauthed(true); }}>Log out</button>
              </div>
            </>
          )}
        </div>
      </header>

      {fatal ? (
        <div className="fatal">Could not load libraries: {fatal}</div>
      ) : (
        <div className="body">
          <ErrorBoundary>
          <LibraryBrowser
            library={libName}
            data={items}
            selectedId={selected?.item_id ?? null}
            onSelect={setSelected}
            loading={itemsLoading}
            error={itemsError}
            dryRun={dryRun}
            libraryRun={libraryRuns[libName] ?? EMPTY_LIBRARY_RUN}
            onStartLibraryRun={(kind) => startLibraryRun(libName, kind)}
            onEditOverlayProfile={openOverlayEditor}
            onSearch={handleSearch}
            onLoadMore={loadMoreItems}
            loadingMore={itemsLoadingMore}
          />
          <PreviewPane
            item={selected}
            plan={plan}
            loading={planLoading}
            error={planError}
            dryRun={dryRun}
            library={libName}
            onCandidateSelected={() => selected && refreshPlan(selected.item_id)}
            onSkipChanged={() => selected && refreshPlan(selected.item_id)}
            onApplyItem={startItemApply}
            itemApplyJob={itemApplyJob}
            itemApplyRunning={itemApplyRunning}
            itemApplyError={itemApplyError}
          />
          </ErrorBoundary>
        </div>
      )}
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { getAuthStatus, getItemsLight, getLibraries, getPlan, startRun, getJob } from './api';
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
  const [itemsError, setItemsError] = useState<string | null>(null);

  const [selected, setSelected] = useState<ItemLightView | null>(null);
  const [plan, setPlan] = useState<PlanResp | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  // S9d4b step 6: item-scoped apply (scope item:<id>).
  const [itemApplyJob, setItemApplyJob] = useState<JobResp | null>(null);
  const [itemApplyRunning, setItemApplyRunning] = useState(false);
  const [itemApplyError, setItemApplyError] = useState<string | null>(null);

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

  const loadLibs = useCallback(() => {
    getLibraries()
      .then((d) => {
        setLibs(d);
        if (d.libraries.length) setLibName(d.libraries[0].name);
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
  ) => {
    setItemsLoading(true);
    setItemsError(null);
    getItemsLight(name)
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
    let cancelled = false;
    loadLibraryItems(libName, () => cancelled, (d) => {
      setSelected((prev) => (prev && d.items.some((i) => i.item_id === prev.item_id) ? prev : null));
    });
    return () => { cancelled = true; };
  }, [libName, unauthed, loadLibraryItems]);

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
      const iv = window.setInterval(async () => {
        try {
          const j = await getJob(r.job_id);
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
    } catch (e) {
      setLibraryRuns((prev) => ({
        ...prev, [library]: { job: null, action: kind, running: false, error: (e as Error).message },
      }));
    }
  }, [libName, onRunDone]);

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
    } catch (e) {
      setItemApplyError((e as Error).message);
      setItemApplyRunning(false);
    }
  }, [selected, libName, onRunDone]);

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

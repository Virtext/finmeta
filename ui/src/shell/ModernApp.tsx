import React, { useEffect, useState } from 'react';
import { ToastProvider } from '../ui';
import { getActivity, getLibraries, logout, onUnauthorized } from '../api';
import LoginScreen from '../components/LoginScreen';
import { useAuthGate } from './useAuthGate';
import { useHashPath } from './router';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import { OverviewPage } from './OverviewPage';
import { LibraryPage } from './LibraryPage';
import { ArtworkPage } from './ArtworkPage';
import { OverlaysPage } from './OverlaysPage';
import { ProfilesPage } from './ProfilesPage';
import { PacksPage } from './PacksPage';
import { AutomationPage } from './AutomationPage';
import { SettingsShell } from './SettingsShell';
import { ActivityShell } from './ActivityShell';
import { CommandPalette } from './CommandPalette';
import { NotBuiltYet } from './NotBuiltYet';
import './shell.css';

const TITLES: Record<string, string> = {
  overview: 'Overview',
  library: 'Library',
  artwork: 'Artwork',
  overlays: 'Overlays',
  profiles: 'Profiles',
  packs: 'Packs',
  automation: 'Automation',
  activity: 'Activity',
  settings: 'Settings',
};

function ModernShell(): React.ReactElement {
  const path = useHashPath();
  const section = path.split('/')[1] || 'overview';
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      const stored = localStorage.getItem('finmeta-theme');
      if (stored === 'light' || stored === 'dark') return stored;
    } catch { /* ignore */ }
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  });
  const [dryRun, setDryRun] = useState<boolean | null>(null);
  const [activeRunLabel, setActiveRunLabel] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('finmeta-theme', theme); } catch { /* ignore */ }
  }, [theme]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-ui', 'modern');
    return () => { document.documentElement.removeAttribute('data-ui'); };
  }, []);

  useEffect(() => {
    getLibraries().then((r) => setDryRun(r.dry_run)).catch(() => {});
    getActivity({ limit: 1 }).then((r) => {
      if (r.active) setActiveRunLabel(`${r.active.action ?? 'run'} · ${r.active.scope ?? 'all'}`);
    }).catch(() => {});
  }, [path]);

  return (
    <div className="fm-modern fm-shell">
      <Sidebar activePath={path} />
      <div className="fm-main">
        <Topbar
          crumb={TITLES[section] ?? section}
          dryRun={dryRun}
          activeRunLabel={activeRunLabel}
          theme={theme}
          onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
          onLogout={() => { logout().catch(() => {}); window.location.reload(); }}
          onOpenSearch={() => setSearchOpen(true)}
        />
        <CommandPalette open={searchOpen} onClose={() => setSearchOpen(false)} />
        <div className="fm-content">
          {section === 'overview' && <OverviewPage />}
          {section === 'library' && <LibraryPage path={path} dryRun={dryRun ?? true} />}
          {section === 'artwork' && <ArtworkPage dryRun={dryRun ?? true} />}
          {section === 'overlays' && <OverlaysPage />}
          {section === 'profiles' && <ProfilesPage />}
          {section === 'packs' && <PacksPage />}
          {section === 'automation' && <AutomationPage />}
          {section === 'settings' && <SettingsShell />}
          {section === 'activity' && <ActivityShell />}
          {!['overview', 'library', 'artwork', 'overlays', 'profiles', 'packs', 'automation', 'settings', 'activity'].includes(section) && (
            <NotBuiltYet title={TITLES[section] ?? section} />
          )}
        </div>
      </div>
    </div>
  );
}

export function ModernApp(): React.ReactElement {
  const { unauthed, setUnauthed } = useAuthGate(() => {});
  // See api.ts's onUnauthorized header comment: without this, a session that
  // expires while already inside the modern shell 401s forever with no way
  // back to the login screen -- none of this shell's pages catch
  // e.isUnauthorized per call site the way LegacyApp.tsx's do.
  useEffect(() => {
    onUnauthorized(() => setUnauthed(true));
    return () => onUnauthorized(null);
  }, [setUnauthed]);
  if (unauthed) {
    return (
      <div className="fm-modern" data-ui="modern">
        <LoginScreen onAuthed={() => setUnauthed(false)} />
      </div>
    );
  }
  return (
    <ToastProvider>
      <ModernShell />
    </ToastProvider>
  );
}

export default ModernApp;

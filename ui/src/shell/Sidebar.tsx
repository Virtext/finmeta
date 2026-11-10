import React from 'react';
import { Icon, IconName } from '../ui';
import { navigate } from './router';

interface NavItem { id: string; label: string; icon: IconName; path: string; }

// IA per SPEC_S14_UI.md §4. Only Overview is wired to a real page right now
// (P2); every other entry routes to an honest "not rebuilt yet" page rather
// than being omitted or faked.
const NAV: NavItem[] = [
  { id: 'overview', label: 'Overview', icon: 'overview', path: '/overview' },
  { id: 'library', label: 'Library', icon: 'library', path: '/library' },
  { id: 'artwork', label: 'Artwork', icon: 'artwork', path: '/artwork' },
  { id: 'overlays', label: 'Overlays', icon: 'overlays', path: '/overlays' },
  { id: 'profiles', label: 'Profiles', icon: 'profiles', path: '/profiles' },
  { id: 'packs', label: 'Packs', icon: 'packs', path: '/packs' },
  { id: 'automation', label: 'Automation', icon: 'automation', path: '/automation' },
  { id: 'activity', label: 'Activity', icon: 'activity', path: '/activity' },
  // 2026-11-10: every scan group needed a modern destination, not just
  // artwork (shell/HealthShell.tsx wraps the shared HealthPage.tsx).
  { id: 'health', label: 'Health', icon: 'health', path: '/health' },
  { id: 'settings', label: 'Settings', icon: 'settings', path: '/settings' },
];

export interface SidebarProps {
  activePath: string;
  /** Below the 900px breakpoint the sidebar is an off-canvas drawer
   * (shell.css) instead of a permanent column -- these control it. */
  mobileOpen?: boolean;
  onMobileClose?: () => void;
}

export function Sidebar({ activePath, mobileOpen, onMobileClose }: SidebarProps): React.ReactElement {
  const activeTop = '/' + activePath.split('/')[1];
  return (
    <>
      {mobileOpen && <div className="fm-sidebar__backdrop" onClick={onMobileClose} />}
      <aside className={['fm-sidebar', mobileOpen ? 'fm-sidebar--open' : ''].filter(Boolean).join(' ')}>
        <button
          type="button"
          className="fm-sidebar__brand"
          onClick={() => navigate('/overview')}
          title="Back to Overview"
        >
          <img src="/finmeta-icon.svg" alt="" className="fm-sidebar__mark" />
          <span className="fm-sidebar__name">Finmeta</span>
        </button>
        <nav>
          {NAV.map((item) => (
            <button
              key={item.id}
              className={['fm-navitem', item.path === activeTop ? 'fm-navitem--active' : ''].filter(Boolean).join(' ')}
              onClick={() => navigate(item.path)}
            >
              <Icon name={item.icon} size={16} className="fm-navitem__icon" />
              {item.label}
            </button>
          ))}
        </nav>
      </aside>
    </>
  );
}

export default Sidebar;

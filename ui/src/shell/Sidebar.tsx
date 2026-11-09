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
  { id: 'settings', label: 'Settings', icon: 'settings', path: '/settings' },
];

export interface SidebarProps {
  activePath: string;
}

export function Sidebar({ activePath }: SidebarProps): React.ReactElement {
  const activeTop = '/' + activePath.split('/')[1];
  return (
    <aside className="fm-sidebar">
      <div className="fm-sidebar__brand">
        <span className="fm-sidebar__mark">F</span>
        <span className="fm-sidebar__name">Finmeta</span>
      </div>
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
  );
}

export default Sidebar;

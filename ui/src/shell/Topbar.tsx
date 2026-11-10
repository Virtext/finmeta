import React from 'react';
import { Badge, Icon, IconButton, Menu } from '../ui';
import { setUiShell } from './uiShell';

export interface TopbarProps {
  crumb: string;
  dryRun: boolean | null;
  activeRunLabel: string | null;
  theme: 'light' | 'dark';
  onToggleTheme: () => void;
  onLogout: () => void;
  onOpenSearch: () => void;
  onOpenNav: () => void;
}

// D3 (SPEC_S14_UI.md §7): still no Notifications bell -- S12e doesn't exist.
// The search box WAS omitted for the same reason until there was something
// real behind it -- see shell/CommandPalette.tsx.
export function Topbar({ crumb, dryRun, activeRunLabel, theme, onToggleTheme, onLogout, onOpenSearch, onOpenNav }: TopbarProps): React.ReactElement {
  return (
    <header className="fm-topbar">
      {/* Mobile-only: the sidebar is hidden below 900px (shell.css) and
          becomes an off-canvas drawer this opens -- there is otherwise no
          way to navigate at all on a phone/tablet width. */}
      <IconButton icon="menu" label="Open navigation" className="fm-topbar__navbtn" onClick={onOpenNav} />
      <div className="fm-topbar__crumb">
        <span>Finmeta</span>
        <span className="fm-topbar__sep">›</span>
        <span className="fm-topbar__here">{crumb}</span>
      </div>
      <button
        onClick={onOpenSearch}
        style={{
          display: 'flex', alignItems: 'center', gap: 8, width: 240, maxWidth: '34%',
          background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
          padding: '5px 9px', color: 'var(--text-faint)', fontSize: 'var(--text-sm)', cursor: 'pointer',
          marginLeft: 16,
        }}
      >
        <Icon name="search" size={13} />
        <span style={{ flex: 1, textAlign: 'left' }}>Search…</span>
        <span style={{ fontFamily: 'var(--ui-mono)', fontSize: 10.5, border: '1px solid var(--border-strong)', borderRadius: 4, padding: '0 4px' }}>⌘K</span>
      </button>
      <div className="fm-topbar__spacer" />
      {activeRunLabel && <Badge tone="accent">{activeRunLabel}</Badge>}
      {dryRun !== null && (
        <Badge tone={dryRun ? 'warn' : 'ok'}>{dryRun ? 'DRY RUN' : 'LIVE'}</Badge>
      )}
      <IconButton
        icon={theme === 'dark' ? 'sun' : 'moon'}
        label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        onClick={onToggleTheme}
      />
      <Menu
        trigger={<IconButton icon="user" label="Account menu" />}
        items={[
          { id: 'legacy', label: 'Switch to classic interface', onSelect: () => setUiShell('legacy') },
          { separator: true },
          { id: 'logout', label: 'Log out', danger: true, onSelect: onLogout },
        ]}
      />
    </header>
  );
}

export default Topbar;

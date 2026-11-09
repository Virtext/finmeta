import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../ui';
import { getHelp, getItemsLight, getLibraries } from '../api';
import { navigate } from './router';

// Global search (⌘K / Ctrl+K). SPEC_S14_UI.md flagged this as a deferred
// "fake affordance" risk until there was something real to search --
// this is that: items across every configured library (client-side
// fan-out over the EXISTING per-library getItemsLight, each call still
// bounded by its own `limit` -- the 2026-10-30 unbounded-fetch timeout is
// exactly the mistake this avoids repeating) plus settings (getHelp()'s
// key + short text, substring match, client-side -- 159 entries is
// nothing to search in-browser).

interface Result {
  kind: 'item' | 'setting';
  key: string;
  title: string;
  subtitle: string;
  go: () => void;
}

const DEBOUNCE_MS = 250;
const MIN_QUERY_LEN = 2;
const MAX_PER_LIBRARY = 6;
const MAX_SETTINGS = 6;

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps): React.ReactElement | null {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Result[]>([]);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<number | null>(null);

  useEffect(() => {
    if (open) {
      setQuery('');
      setResults([]);
      setActive(0);
      // rendered this tick; focus next tick so the input actually exists
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    if (query.trim().length < MIN_QUERY_LEN) { setResults([]); setLoading(false); return; }
    setLoading(true);
    debounceRef.current = window.setTimeout(() => {
      const q = query.trim();
      const qLower = q.toLowerCase();

      const itemsPromise = getLibraries().then((libs) =>
        Promise.all(libs.libraries.map((l) =>
          getItemsLight(l.name, { limit: MAX_PER_LIBRARY, offset: 0, search: q })
            .then((r) => r.items.map((it): Result => ({
              kind: 'item',
              key: `item:${it.item_id}`,
              title: it.item_name,
              subtitle: l.name,
              go: () => navigate(`/library/${encodeURIComponent(l.name)}/item/${encodeURIComponent(it.item_id)}`),
            })))
            .catch(() => [] as Result[]),
        )),
      ).then((perLib) => perLib.flat());

      const settingsPromise = getHelp().then((r) => {
        const matches = Object.entries(r.help)
          .filter(([key, h]) => key.toLowerCase().includes(qLower) || h.short.toLowerCase().includes(qLower))
          .slice(0, MAX_SETTINGS)
          .map(([key, h]): Result => ({
            kind: 'setting',
            key: `setting:${key}`,
            title: key,
            subtitle: h.short,
            go: () => navigate('/settings'),
          }));
        return matches;
      }).catch(() => [] as Result[]);

      Promise.all([itemsPromise, settingsPromise])
        .then(([items, settings]) => { setResults([...items, ...settings]); setActive(0); })
        .finally(() => setLoading(false));
    }, DEBOUNCE_MS);
    return () => { if (debounceRef.current) window.clearTimeout(debounceRef.current); };
  }, [query]);

  const select = (r: Result) => { r.go(); onClose(); };

  const grouped = useMemo(() => ({
    items: results.filter((r) => r.kind === 'item'),
    settings: results.filter((r) => r.kind === 'setting'),
  }), [results]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { onClose(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, results.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); if (results[active]) select(results[active]); }
  };

  return (
    <div
      className="fm-overlay"
      style={{ alignItems: 'flex-start', paddingTop: '12vh' }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="fm-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        style={{ margin: '0 auto', padding: 0, overflow: 'hidden' }}
        onKeyDown={onKeyDown}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
          <Icon name="search" size={15} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search titles or settings…"
            style={{ flex: 1, background: 'none', border: 'none', outline: 'none', color: 'var(--text)', fontSize: 'var(--text-md)' }}
          />
          <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)', border: '1px solid var(--border-strong)', borderRadius: 4, padding: '1px 5px' }}>Esc</span>
        </div>

        <div style={{ maxHeight: '50vh', overflowY: 'auto' }}>
          {query.trim().length < MIN_QUERY_LEN ? (
            <div style={{ padding: '20px 16px', color: 'var(--text-faint)', fontSize: 'var(--text-sm)' }}>
              Type at least {MIN_QUERY_LEN} characters to search titles across every library, or settings.
            </div>
          ) : loading && results.length === 0 ? (
            <div style={{ padding: '20px 16px', color: 'var(--text-faint)', fontSize: 'var(--text-sm)' }}>Searching…</div>
          ) : results.length === 0 ? (
            <div style={{ padding: '20px 16px', color: 'var(--text-faint)', fontSize: 'var(--text-sm)' }}>No matches for "{query}".</div>
          ) : (
            <>
              {grouped.items.length > 0 && (
                <ResultGroup label="Titles" items={grouped.items} results={results} active={active} onSelect={select} />
              )}
              {grouped.settings.length > 0 && (
                <ResultGroup label="Settings" items={grouped.settings} results={results} active={active} onSelect={select} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ResultGroup({
  label, items, results, active, onSelect,
}: { label: string; items: Result[]; results: Result[]; active: number; onSelect: (r: Result) => void }): React.ReactElement {
  return (
    <div>
      <div style={{ padding: '8px 16px 4px', fontSize: 'var(--text-xs)', color: 'var(--text-faint)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      {items.map((r) => {
        const isActive = results[active]?.key === r.key;
        return (
          <button
            key={r.key}
            onClick={() => onSelect(r)}
            style={{
              display: 'flex', flexDirection: 'column', width: '100%', textAlign: 'left',
              padding: '8px 16px', background: isActive ? 'var(--surface-3)' : 'transparent',
              border: 'none', cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            <span style={{ fontSize: 'var(--text-sm)', color: 'var(--text)' }}>{r.title}</span>
            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-faint)' }}>{r.subtitle}</span>
          </button>
        );
      })}
    </div>
  );
}

export default CommandPalette;

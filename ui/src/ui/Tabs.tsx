import React, { useRef } from 'react';

export interface TabItem { id: string; label: string; icon?: React.ReactNode; }

export interface TabsProps {
  items: TabItem[];
  activeId: string;
  onChange: (id: string) => void;
  'aria-label': string;
}

// Roving tabindex + arrow-key navigation (SPEC_S14_UI.md §2.5): only the
// active tab is in the normal tab order, Left/Right move focus AND
// selection between tabs, Home/End jump to the ends.
export function Tabs({ items, activeId, onChange, ...rest }: TabsProps): React.ReactElement {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const move = (fromIndex: number, delta: number) => {
    const next = (fromIndex + delta + items.length) % items.length;
    const id = items[next].id;
    onChange(id);
    refs.current[id]?.focus();
  };

  return (
    <div className="fm-tabs" role="tablist" aria-label={rest['aria-label']}>
      {items.map((item, i) => (
        <button
          key={item.id}
          ref={(el) => { refs.current[item.id] = el; }}
          role="tab"
          type="button"
          aria-selected={item.id === activeId}
          tabIndex={item.id === activeId ? 0 : -1}
          className={['fm-tab', item.id === activeId ? 'fm-tab--active' : ''].filter(Boolean).join(' ')}
          onClick={() => onChange(item.id)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowRight') { e.preventDefault(); move(i, 1); }
            else if (e.key === 'ArrowLeft') { e.preventDefault(); move(i, -1); }
            else if (e.key === 'Home') { e.preventDefault(); move(i, -i); }
            else if (e.key === 'End') { e.preventDefault(); move(i, items.length - 1 - i); }
          }}
        >
          {item.icon}
          {item.label}
        </button>
      ))}
    </div>
  );
}

export default Tabs;

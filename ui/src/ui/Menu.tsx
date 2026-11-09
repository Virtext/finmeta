import React, { useEffect, useRef, useState } from 'react';

export interface MenuItemSpec {
  id: string;
  label: string;
  danger?: boolean;
  onSelect: () => void;
}

export type MenuEntry = MenuItemSpec | { separator: true };

export interface MenuProps {
  trigger: React.ReactNode;
  items: MenuEntry[];
  align?: 'left' | 'right';
}

export function Menu({ trigger, items, align = 'right' }: MenuProps): React.ReactElement {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="fm-menu-wrap" ref={wrapRef}>
      <span onClick={() => setOpen((v) => !v)}>{trigger}</span>
      {open && (
        <div className="fm-menu" role="menu" style={align === 'left' ? { right: 'auto', left: 0 } : undefined}>
          {items.map((item, i) =>
            'separator' in item ? (
              <div key={`sep-${i}`} className="fm-menu__sep" role="separator" />
            ) : (
              <button
                key={item.id}
                role="menuitem"
                type="button"
                className={['fm-menu__item', item.danger ? 'fm-menu__item--danger' : ''].filter(Boolean).join(' ')}
                onClick={() => { setOpen(false); item.onSelect(); }}
              >
                {item.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export default Menu;

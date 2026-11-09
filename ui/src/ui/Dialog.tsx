import React, { useEffect, useRef } from 'react';

function useFocusTrapAndEsc(active: boolean, onClose: () => void, containerRef: React.RefObject<HTMLElement>) {
  useEffect(() => {
    if (!active) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const container = containerRef.current;
    const focusables = () =>
      container?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ) ?? [];
    focusables()[0]?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); return; }
      if (e.key !== 'Tab') return;
      const nodes = Array.from(focusables());
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = prevOverflow;
      previouslyFocused?.focus();
    };
  }, [active, onClose, containerRef]);
}

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children?: React.ReactNode;
  actions?: React.ReactNode;
}

export function Dialog({ open, onClose, title, children, actions }: DialogProps): React.ReactElement | null {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrapAndEsc(open, onClose, ref);
  if (!open) return null;
  return (
    <div className="fm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-dialog" role="dialog" aria-modal="true" aria-labelledby="fm-dialog-title" ref={ref}>
        <h2 className="fm-dialog__title" id="fm-dialog-title">{title}</h2>
        {children && <div className="fm-dialog__body">{children}</div>}
        {actions && <div className="fm-dialog__actions">{actions}</div>}
      </div>
    </div>
  );
}

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children?: React.ReactNode;
}

export function Drawer({ open, onClose, title, children }: DrawerProps): React.ReactElement | null {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrapAndEsc(open, onClose, ref);
  if (!open) return null;
  return (
    <div className="fm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        {children}
      </div>
    </div>
  );
}

export default Dialog;

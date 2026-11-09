import React, { createContext, useCallback, useContext, useState } from 'react';
import Icon from './Icon';

export type ToastTone = 'ok' | 'warn' | 'danger';

export interface ToastSpec {
  id: string;
  message: string;
  tone?: ToastTone;
  actionLabel?: string;
  onAction?: () => void;
}

interface ToastContextValue {
  push: (toast: Omit<ToastSpec, 'id'>) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast() must be used inside <ToastProvider>');
  return ctx;
}

const AUTO_DISMISS_MS = 5000;

export function ToastProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  const [toasts, setToasts] = useState<ToastSpec[]>([]);

  const dismiss = useCallback((id: string) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((toast: Omit<ToastSpec, 'id'>) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setToasts((ts) => [...ts, { ...toast, id }]);
    window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
  }, [dismiss]);

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="fm-toast-region" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={['fm-toast', t.tone && t.tone !== 'ok' ? `fm-toast--${t.tone}` : ''].filter(Boolean).join(' ')}>
            <span>{t.message}</span>
            {t.actionLabel && (
              <button className="fm-toast__action" onClick={() => { t.onAction?.(); dismiss(t.id); }}>
                {t.actionLabel}
              </button>
            )}
            <button className="fm-toast__close" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
              <Icon name="close" size={12} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export default ToastProvider;

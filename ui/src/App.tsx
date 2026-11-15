import React, { useEffect, useState } from 'react';
import LegacyApp from './shell/LegacyApp';
import ModernApp from './shell/ModernApp';
import { getUiShell } from './shell/uiShell';
import { getAuthStatus } from './api';

// Fase C: FINMETA_AUTH=off must never go unnoticed -- shown above either shell.
function AuthOffBanner(): React.ReactElement | null {
  const [off, setOff] = useState(false);
  useEffect(() => { getAuthStatus().then((s) => setOff(!s.auth_enabled)).catch(() => {}); }, []);
  if (!off) return null;
  return (
    <div role="alert" style={{ background: 'var(--danger)', color: '#fff', padding: '6px 16px',
                               fontWeight: 600, textAlign: 'center' }}>
      Authentication is disabled (FINMETA_AUTH=off) — anyone who can reach this page has full
      admin access. Only use this for local development.
    </div>
  );
}

// SPEC_S14_UI.md §4b: the operator's explicit requirement is that the old
// interface is never deleted. This is the whole selector -- everything
// else lives in shell/LegacyApp.tsx (today's App.tsx, moved verbatim) and
// shell/ModernApp.tsx (the redesign).
export default function App(): React.ReactElement {
  return (
    <>
      <AuthOffBanner />
      {getUiShell() === 'modern' ? <ModernApp /> : <LegacyApp />}
    </>
  );
}

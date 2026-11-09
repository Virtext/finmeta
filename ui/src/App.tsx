import React from 'react';
import LegacyApp from './shell/LegacyApp';
import ModernApp from './shell/ModernApp';
import { getUiShell } from './shell/uiShell';

// SPEC_S14_UI.md §4b: the operator's explicit requirement is that the old
// interface is never deleted. This is the whole selector -- everything
// else lives in shell/LegacyApp.tsx (today's App.tsx, moved verbatim) and
// shell/ModernApp.tsx (the redesign).
export default function App(): React.ReactElement {
  return getUiShell() === 'modern' ? <ModernApp /> : <LegacyApp />;
}

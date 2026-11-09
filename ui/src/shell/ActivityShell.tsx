import React from 'react';
import ActivityPage from '../components/ActivityPage';
import { navigate } from './router';

// Shared panel (SPEC_S14_UI.md §4b), same reasoning as SettingsShell.tsx:
// ActivityPage renders its own "← Back" header, so no PageHeader wrapper
// here either -- stacking a second one would just duplicate it.
export function ActivityShell(): React.ReactElement {
  return <ActivityPage onBack={() => navigate('/overview')} />;
}

export default ActivityShell;
